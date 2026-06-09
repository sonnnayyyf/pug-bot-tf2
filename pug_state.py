"""
TF2 6s PUG bot — core state machine.

Phases:
  IDLE -> QUEUING -> READY_CHECK -> PICKING -> LIVE -> (report) -> IDLE

Auto-ready: every join arms an auto-confirm window. When the queue fills,
anyone whose window is still open is auto-confirmed; everyone else must
ready up within READY_CHECK_SECONDS or they're dropped.

In-memory only (resets on restart, by choice). No discord import here so
the whole thing is unit-testable. Every public method returns (ok, msg)
unless noted; ok=False means a guard rejected it.
"""

import random
import time
from enum import Enum, auto


class Phase(Enum):
    IDLE = auto()
    QUEUING = auto()
    READY_CHECK = auto()   # 12 reached, confirming everyone is here
    PICKING = auto()       # all ready, captains rolled, drafting
    LIVE = auto()


QUEUE_SIZE = 12
TIMEOUT_SECONDS = 2 * 60 * 60    # idle drop while QUEUING (2 hours)
IMMUNITY_GAMES = 2               # med-immunity granted on medding (no stacking)
DEFAULT_AR_SECONDS = 2 * 60      # ++ / /add
AR_COMMAND_SECONDS = 15 * 60     # !ar / /ar
MAX_AR_SECONDS = 30 * 60         # /auto-ready hard cap
READY_CHECK_SECONDS = 120        # window to ready up once queue is full (2 min)
LIVE_AUTO_REPORT_SECONDS = 50 * 60  # auto-end a live game if no one reports (50 min)

# RED first. (team, picks_this_turn). 1-2-1-1-1-1-1-1-1 -> 5/5 picks = 6v6.
PICK_ORDER = [
    ("RED", 1), ("BLU", 2), ("RED", 1), ("BLU", 1), ("RED", 1),
    ("BLU", 1), ("RED", 1), ("BLU", 1), ("RED", 1),
]


class PugState:
    def __init__(self, now=time.time, immunity=None):
        self._now = now
        # immunity may be a dict shared across several PugStates (multi-lobby:
        # immunity is a property of the player, not the channel). We only ever
        # mutate it in place so the shared reference stays intact.
        self.immunity = immunity if immunity is not None else {}
        self.auto_ready_until = {}    # uid -> epoch its auto-ready window ends
        self.next_queue = {}          # uid -> joined_at; players waiting for the NEXT game
        self.reset_match()

    def reset_match(self):
        """Reset match state. Keeps immunity and auto-ready (both time/cross-game)."""
        self.phase = Phase.IDLE
        self.queue = {}               # uid -> joined_at
        self.ready = set()            # uids confirmed for the current ready check
        self.captains = []
        self.team = {"RED": [], "BLU": []}
        self.capt_of = {}
        self.turn_idx = 0
        self.picks_left = 0
        self.sub_requests = []
        self.live_since = None        # epoch the game went LIVE (for auto-report)

    def reset_all(self):
        self.immunity.clear()
        self.auto_ready_until = {}
        self.next_queue = {}
        self.reset_match()

    # ---------- serialization (for persistence; pure, no DB here) ----------
    def to_dict(self):
        """All state as JSON-safe primitives. The bot persists this snapshot."""
        return {
            "phase": self.phase.name,
            "queue": self.queue,
            "ready": list(self.ready),
            "captains": list(self.captains),
            "team": {"RED": list(self.team["RED"]), "BLU": list(self.team["BLU"])},
            "capt_of": dict(self.capt_of),
            "turn_idx": self.turn_idx,
            "picks_left": self.picks_left,
            "sub_requests": list(self.sub_requests),
            "immunity": self.immunity,
            "auto_ready_until": self.auto_ready_until,
            "next_queue": self.next_queue,
            "live_since": self.live_since,
        }

    def load_dict(self, d):
        """Restore a snapshot from to_dict. Tolerant of JSON's string dict keys
        (uids come back as ints either way). Unknown/missing fields fall back to
        empty defaults so an old or partial snapshot can't crash startup."""
        def ints(m):                       # uid-keyed map -> int keys + int/num vals kept
            return {int(k): v for k, v in (m or {}).items()}
        try:
            self.phase = Phase[d.get("phase", "IDLE")]
        except KeyError:
            self.phase = Phase.IDLE
        self.queue = ints(d.get("queue"))
        self.ready = {int(u) for u in d.get("ready", [])}
        self.captains = [int(u) for u in d.get("captains", [])]
        team = d.get("team") or {}
        self.team = {"RED": [int(u) for u in team.get("RED", [])],
                     "BLU": [int(u) for u in team.get("BLU", [])]}
        self.capt_of = {k: int(v) for k, v in (d.get("capt_of") or {}).items()}
        self.turn_idx = int(d.get("turn_idx", 0))
        self.picks_left = int(d.get("picks_left", 0))
        self.sub_requests = [int(u) for u in d.get("sub_requests", [])]
        self.immunity.clear()
        self.immunity.update(ints(d.get("immunity")))
        self.auto_ready_until = ints(d.get("auto_ready_until"))
        self.next_queue = ints(d.get("next_queue"))
        self.live_since = d.get("live_since")

    # ---------- helpers ----------
    @property
    def _both_capts_set(self):
        return len(self.capt_of) == 2

    def _all_players(self):
        return (list(self.team["RED"]) + list(self.team["BLU"])
                + list(self.capt_of.values()))

    def _drafted(self):
        return set(self._all_players())

    def _current_team(self):
        return PICK_ORDER[self.turn_idx][0]

    def _current_picker(self):
        return self.capt_of.get(self._current_team())

    def queue_ids(self):
        return list(self.queue.keys())

    def next_queue_ids(self):
        return list(self.next_queue.keys())

    @property
    def slot_busy(self):
        """True when a match is forming or live (so new joins wait for next)."""
        return self.phase in (Phase.READY_CHECK, Phase.PICKING, Phase.LIVE)

    def unpicked(self):
        drafted = self._drafted()
        return [u for u in self.queue if u not in drafted]

    # ---------- queue + auto-ready ----------
    def add(self, uid, ar_seconds=DEFAULT_AR_SECONDS):
        """Join the active queue, or the next queue if a match is in progress.
        Re-arms auto-ready if already queued."""
        ar_seconds = min(ar_seconds, MAX_AR_SECONDS)
        self.auto_ready_until[uid] = self._now() + ar_seconds
        # Already in a live ready check: arming auto-ready confirms them now AND
        # (since the window is open) re-confirms them automatically if the check
        # has to restart after a timeout/abort — so they never re-click Ready.
        if self.phase == Phase.READY_CHECK and uid in self.queue:
            return self.mark_ready(uid)
        if self.slot_busy:
            if uid in self.queue:
                return False, "You're in the current game. /match report to end it first."
            if uid in self.next_queue:
                return True, f"Auto-ready extended. (next queue {len(self.next_queue)}/{QUEUE_SIZE})"
            if len(self.next_queue) >= QUEUE_SIZE:
                return False, "The next queue is full — wait for the current game to finish."
            self.next_queue[uid] = self._now()
            return True, f"next ({len(self.next_queue)}/{QUEUE_SIZE})"
        # active slot free
        if uid in self.queue:
            return True, f"Auto-ready extended. ({len(self.queue)}/{QUEUE_SIZE})"
        self.phase = Phase.QUEUING
        self.queue[uid] = self._now()
        if len(self.queue) >= QUEUE_SIZE:
            return self._begin_ready_check()
        return True, f"Queued ({len(self.queue)}/{QUEUE_SIZE})."

    def clear_auto_ready(self, uid):
        """Turn off a player's auto-confirm; they must ready up manually."""
        if uid in self.next_queue:
            self.auto_ready_until[uid] = 0
            return True, "Auto-ready off for the next game."
        if self.phase == Phase.QUEUING and uid in self.queue:
            self.auto_ready_until[uid] = 0
            return True, "Auto-ready off — you'll need to ready up manually."
        return False, "Only works while you're queuing."

    def remove(self, uid):
        if uid in self.next_queue:
            del self.next_queue[uid]
            return True, "Left the next queue."
        if uid in self.queue:
            if self.phase == Phase.QUEUING:
                del self.queue[uid]
                if not self.queue:
                    self.phase = Phase.IDLE
                return True, "Left the queue."
            return False, "You're in the current game — /match report to end it, or /subme."
        return False, "You're not in a queue."

    def sweep_timeouts(self):
        """Idle-drop sweep. Background loop. Sweeps the active queue (only while
        QUEUING) and the next queue (any time). Returns dropped uids."""
        now, dropped = self._now(), []
        if self.phase == Phase.QUEUING:
            for uid, joined in list(self.queue.items()):
                if now - joined >= TIMEOUT_SECONDS:
                    del self.queue[uid]
                    dropped.append(uid)
            if not self.queue:
                self.phase = Phase.IDLE
        for uid, joined in list(self.next_queue.items()):
            if now - joined >= TIMEOUT_SECONDS:
                del self.next_queue[uid]
                dropped.append(uid)
        return dropped

    # ---------- ready check ----------
    def _begin_ready_check(self):
        self.phase = Phase.READY_CHECK
        now = self._now()
        # auto-confirm anyone whose auto-ready window is still open
        self.ready = {u for u in self.queue if self.auto_ready_until.get(u, 0) > now}
        if len(self.ready) == len(self.queue):
            return self._start_draft()
        return True, "ready_check"

    def mark_ready(self, uid):
        if self.phase != Phase.READY_CHECK:
            return False, "No ready check active."
        if uid not in self.queue:
            return False, "You're not in this match."
        self.ready.add(uid)
        if len(self.ready) == len(self.queue):
            return self._start_draft()
        return True, "ready"

    def ready_status(self):
        ready = [u for u in self.queue if u in self.ready]
        not_ready = [u for u in self.queue if u not in self.ready]
        return ready, not_ready

    def _merge_next_into_active(self):
        """Fold next-queue waiters into the active queue when a forming match
        collapses, but NEVER past QUEUE_SIZE. Promotes oldest-joined first; any
        overflow stays in the next queue. May re-trigger a ready check / draft
        if the active queue now has 12."""
        slots = QUEUE_SIZE - len(self.queue)
        for uid, joined in sorted(self.next_queue.items(), key=lambda kv: kv[1]):
            if slots <= 0:
                break
            if uid in self.queue:
                del self.next_queue[uid]          # dedupe; doesn't consume a slot
                continue
            self.queue[uid] = joined
            del self.next_queue[uid]
            slots -= 1
        if len(self.queue) >= QUEUE_SIZE:
            self._begin_ready_check()
        else:
            self.phase = Phase.QUEUING if self.queue else Phase.IDLE

    def abort_ready_check(self, uid):
        """A participant bails during the ready check. ONLY they leave; everyone
        else stays. The queue is no longer full, so we fold in any next-queue
        waiters and drop back to QUEUING (or re-fire a check if that hits 12).
        Returns (ok, msg, dropped)."""
        if self.phase != Phase.READY_CHECK:
            return False, "No ready check active.", []
        if uid not in self.queue:
            return False, "You're not in this match.", []
        del self.queue[uid]
        self.ready = set()
        self.phase = Phase.QUEUING if self.queue else Phase.IDLE
        self._merge_next_into_active()
        return True, "aborted", [uid]

    def resolve_ready_check(self):
        """Called when the ready window expires. Drops the not-ready, then folds
        in any next-queue waiters. Returns the dropped uids."""
        if self.phase != Phase.READY_CHECK:
            return []
        not_ready = [u for u in self.queue if u not in self.ready]
        for u in not_ready:
            del self.queue[u]
        self.ready = set()
        self.phase = Phase.QUEUING if self.queue else Phase.IDLE
        self._merge_next_into_active()
        return not_ready

    # ---------- draft ----------
    def _start_draft(self):
        players = list(self.queue.keys())
        self.captains = self._roll_captains(players)
        self.phase = Phase.PICKING
        self.turn_idx = 0
        self.picks_left = PICK_ORDER[0][1]
        self.ready = set()
        return True, "draft"

    def _roll_captains(self, players):
        free = [p for p in players if p not in self.immunity]
        immune = [p for p in players if p in self.immunity]
        random.shuffle(free); random.shuffle(immune)
        return (free + immune)[:2]

    def capfor(self, uid, color):
        if self.phase != Phase.PICKING:
            return False, "Not in picking phase."
        if uid not in self.queue:
            return False, "You're not in this match."
        if uid in self.capt_of.values():
            return False, "You've already claimed a side."
        color = color.upper()
        if color not in ("RED", "BLU"):
            return False, "Pick red or blu."
        if color in self.capt_of:                 # that side taken; try the other
            color = "BLU" if "RED" in self.capt_of else "RED"
            if color in self.capt_of:
                return False, "Both sides already have captains."
        self.capt_of[color] = uid
        if not self._both_capts_set:
            return True, f"<@{uid}> is {color}. Waiting for the other captain."
        red, blu = self.capt_of["RED"], self.capt_of["BLU"]
        return True, (f"RED <@{red}> vs BLU <@{blu}>. "
                      f"<@{self._current_picker()}> picks first.")

    def uncap(self, uid):
        """Step down from captaincy, freeing the slot for someone else."""
        if self.phase != Phase.PICKING:
            return False, "Not in picking phase."
        for color, c in list(self.capt_of.items()):
            if c == uid:
                del self.capt_of[color]
                return True, (f"<@{uid}> stepped down as {color} captain. "
                              f"/capfor {color.lower()} to take it.")
        return False, "You're not a captain."

    def _advance_pick(self):
        """Consume the current pick slot; move to the next, or go live if done."""
        if self.picks_left == 0:
            self.turn_idx += 1
            if self.turn_idx >= len(PICK_ORDER):
                self._go_live()
            else:
                self.picks_left = PICK_ORDER[self.turn_idx][1]

    def pick(self, uid, target):
        if self.phase != Phase.PICKING:
            return False, "Not in picking phase."
        if not self._both_capts_set:
            return False, "Both captains must /capfor first."
        if uid != self._current_picker():
            return False, "Not your turn."
        if target not in self.queue:
            return False, "That player isn't in this game."
        if target in self._drafted():
            return False, "Already picked."
        team = self._current_team()
        self.team[team].append(target)
        self.picks_left -= 1
        self._advance_pick()
        if self.phase is Phase.LIVE:
            return True, f"<@{target}> -> {team}. Teams set. GLHF!"
        # Only one player left? Their team is forced, so auto-assign instead of
        # making the other captain waste a turn on a no-choice pick.
        remaining = self.unpicked()
        if len(remaining) == 1:
            last, last_team = remaining[0], self._current_team()
            self.team[last_team].append(last)
            self.picks_left -= 1
            self._advance_pick()
            return True, (f"<@{target}> -> {team}. <@{last}> auto-assigned to {last_team}. "
                          "Teams set — GLHF!")
        return True, f"<@{target}> -> {team}. Next: <@{self._current_picker()}>."

    def _go_live(self):
        self._apply_immunity()
        self.phase = Phase.LIVE
        self.live_since = self._now()
        return True, "Teams set. GLHF!"

    def check_live_timeout(self):
        """Auto-end a live game that's run past the report window (handles
        captains forgetting to /match report). Promotes any next queue, same as
        a manual report. Returns the finished game's players, or None if nothing
        timed out."""
        if self.phase is not Phase.LIVE or self.live_since is None:
            return None
        if self._now() - self.live_since < LIVE_AUTO_REPORT_SECONDS:
            return None
        players = list(self.queue.keys())
        self._end_and_promote()
        return players

    def _apply_immunity(self):
        caps = set(self.capt_of.values())     # whoever actually captained/medded
        for p in self._all_players():
            if p in caps:
                self.immunity[p] = IMMUNITY_GAMES        # refresh, capped, no stack
            elif p in self.immunity:
                self.immunity[p] -= 1
                if self.immunity[p] <= 0:
                    del self.immunity[p]

    # ---------- subs ----------
    def request_sub(self, uid):
        if self.phase != Phase.PICKING:
            return False, "Subs can only be used during the draft stage."
        if uid not in self.queue:
            return False, "You're not in a queue."
        if uid in self.sub_requests:
            return False, "You already requested a sub."
        self.sub_requests.append(uid)
        return True, f"<@{uid}> wants a sub. Use /subfor to take the spot."

    def sub_for(self, new_uid, target_uid=None):
        if self.phase != Phase.PICKING:
            return False, "Subs can only be used during the draft stage."
        if new_uid in self.queue:
            return False, "You're already in this game."
        if not self.sub_requests:
            return False, "No open sub spots right now."
        if target_uid is not None:
            if target_uid not in self.sub_requests:
                return False, "That player hasn't requested a sub."
            target = target_uid
        else:
            target = self.sub_requests[0]
        self._swap(target, new_uid)
        self.sub_requests.remove(target)
        return True, f"<@{new_uid}> subbed in for <@{target}>."

    def _swap(self, out_uid, in_uid):
        if out_uid in self.queue:
            self.queue[in_uid] = self.queue.pop(out_uid)
        self.ready.discard(out_uid)
        for color in ("RED", "BLU"):
            if out_uid in self.team[color]:
                self.team[color] = [in_uid if x == out_uid else x for x in self.team[color]]
            if self.capt_of.get(color) == out_uid:
                self.capt_of[color] = in_uid
        self.captains = [in_uid if x == out_uid else x for x in self.captains]

    # ---------- end / cancel / admin ----------
    def match_report(self, uid, is_admin=False):
        """Report a LIVE game (captain/admin) OR cancel a forming match (admin).
        Returns (ok, msg, players_to_ping)."""
        if self.phase == Phase.LIVE:
            if not is_admin and uid not in self.capt_of.values():
                return False, "Only a captain or admin can report.", []
            players = list(self.queue.keys())
            self._end_and_promote()
            return True, "Game reported. Queue open — /add to join.", players
        if self.phase in (Phase.READY_CHECK, Phase.PICKING):
            if not is_admin:
                return False, "Only admins can cancel a forming match.", []
            players = list(self.queue.keys())
            self._end_and_promote()
            return True, "your match has been canceled.", players
        return False, "No match to report.", []

    def _end_and_promote(self):
        """Clear the finished match and promote the next queue into the slot.
        (Immunity was already applied at go-live; cancels apply none.)"""
        self.reset_match()                  # clears active match; keeps next_queue/immunity
        if self.next_queue:
            self.queue = self.next_queue
            self.next_queue = {}
            if len(self.queue) >= QUEUE_SIZE:
                self._begin_ready_check()   # promoted queue already full -> ready check
            else:
                self.phase = Phase.QUEUING

    def match_put(self, uid, where):
        """Admin: move a player onto a team, or to the bench (off all teams).
        Used to rebalance. Moving a captain steps them down from captaincy first
        (their old side is left open — assign a new captain with /capfor)."""
        if self.phase not in (Phase.PICKING, Phase.LIVE):
            return False, "No teams to move players between yet."
        where = where.upper()
        if where not in ("RED", "BLU", "BENCH"):
            return False, "Target must be red, blu, or bench."
        if uid not in self.queue:
            return False, "That player isn't in this match."
        was_capt = None
        for color, c in list(self.capt_of.items()):     # if a captain, free the slot
            if c == uid:
                was_capt = color
                del self.capt_of[color]
        for c in ("RED", "BLU"):
            if uid in self.team[c]:
                self.team[c].remove(uid)
        note = f" (stepped down as {was_capt} captain)" if was_capt else ""
        if where == "BENCH":
            return True, f"Moved <@{uid}> to the bench{note}."
        if uid not in self.team[where]:
            self.team[where].append(uid)
        return True, f"Moved <@{uid}> to {where}{note}."

    # ---------- immunity admin ----------
    def immunity_list(self):
        return dict(self.immunity)

    def set_immunity(self, uid, games):
        games = max(0, int(games))
        if games == 0:
            self.immunity.pop(uid, None)
        else:
            self.immunity[uid] = games
        return True, f"<@{uid}> med-immunity set to {games}."

    def add_immunity(self, uid, delta):
        return self.set_immunity(uid, self.immunity.get(uid, 0) + int(delta))

    def clear_immunity(self, uid):
        had = self.immunity.pop(uid, None)
        return True, (f"<@{uid}> med-immunity cleared." if had
                      else f"<@{uid}> had no immunity.")

    def admin_reset(self):
        """Unstick a frozen pick: same 12 players, re-roll captains."""
        players = dict(self.queue)
        self.reset_match()
        self.queue = players
        if len(self.queue) >= QUEUE_SIZE:
            return self._start_draft()
        self.phase = Phase.QUEUING if self.queue else Phase.IDLE
        return True, "Reset to queue."

    def admin_clear(self):
        """Clear the queue. While a game is forming/live, the only thing that's
        actually a 'queue' is the next queue, so clear ONLY that and leave the
        current game alone (use /match report to end a game). With no game in
        progress, clear the active queue as before."""
        if self.slot_busy:
            had = len(self.next_queue)
            self.next_queue = {}
            return True, (f"Next queue cleared ({had} removed). The current game is "
                          "untouched — use /match report to end it.")
        self.reset_match()
        self.next_queue = {}
        return True, "Queue cleared."


# --------------------------------------------------------------------------
# Smoke test (python pug_state.py). Seed for the Pytest suite.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    clock = {"t": 1000.0}
    NOW = lambda: clock["t"]
    P = list(range(1, 13))

    def fresh():
        return PugState(now=NOW)

    def fill(s, players, ar=DEFAULT_AR_SECONDS):
        for p in players:
            s.add(p, ar)

    def drive_draft(s):
        caps = list(s.captains)
        s.capfor(caps[0], "red"); s.capfor(caps[1], "blu")
        pool = [p for p in P if p not in caps]
        while s.phase is Phase.PICKING:
            s.pick(s._current_picker(), pool.pop())
        return caps

    # A) everyone auto-ready -> straight to draft, no ready check
    clock["t"] = 1000.0
    s = fresh(); fill(s, P)
    assert s.phase is Phase.PICKING, s.phase
    caps = drive_draft(s)
    assert s.phase is Phase.LIVE
    assert len(s.team["RED"]) == 5 and len(s.team["BLU"]) == 5
    assert s.immunity == {caps[0]: 2, caps[1]: 2}
    print("A) all auto-ready -> draft -> 6v6, meds immune x2")

    # match_report by captain ends the live game
    ok, msg, pinged = s.match_report(caps[0])
    assert ok and s.phase is Phase.IDLE and len(pinged) == 12
    print("   captain /match report ends live game, pings 12")

    # B) ready check: 11 join early (AR expires), 12th joins late
    clock["t"] = 1000.0
    s = fresh(); fill(s, P[:11])              # AR until 1120
    clock["t"] = 1200.0                       # those 11 now expired
    s.add(P[11])                              # 12th joins, AR until 1320
    assert s.phase is Phase.READY_CHECK, s.phase
    ready, not_ready = s.ready_status()
    assert ready == [P[11]] and len(not_ready) == 11
    print("B) ready check fired; only the fresh joiner auto-confirmed")

    # 10 of the laggards ready up; 1 never does -> dropped on resolve
    for p in P[:10]:
        s.mark_ready(p)
    dropped = s.resolve_ready_check()
    assert dropped == [P[10]], dropped
    assert s.phase is Phase.QUEUING and len(s.queue) == 11
    print("   not-ready player dropped, back to 11/12")

    # C) marking the last player ready flips straight to draft
    clock["t"] = 1000.0
    s = fresh(); fill(s, P[:11])
    clock["t"] = 1200.0
    s.add(P[11])                              # ready check, only 12 confirmed
    for p in P[:11]:
        s.mark_ready(p)                       # 11th mark completes it
    assert s.phase is Phase.PICKING, s.phase
    print("C) last ready-up completes check -> draft")

    # D) admin cancel mid-pick; non-admin blocked
    ok, _, _ = s.match_report(99999, is_admin=False)
    assert ok is False
    ok, msg, pinged = s.match_report(99999, is_admin=True)
    assert ok and "canceled" in msg and len(pinged) == 12 and s.phase is Phase.IDLE
    print("D) admin /match report cancels mid-pick; non-admin blocked")

    # E) auto-ready cap at 30 min
    clock["t"] = 0.0
    s = fresh(); s.add(1, ar_seconds=99 * 60)
    assert s.auto_ready_until[1] == MAX_AR_SECONDS, s.auto_ready_until[1]
    print("E) auto-ready capped at 30m")

    # F) immunity excludes from captain roll
    s2 = fresh(); s2.immunity = {1: 2, 2: 1}
    assert 1 not in s2._roll_captains(P) and 2 not in s2._roll_captains(P)
    print("F) immune players excluded from captain roll")

    # G) subs only work in PICKING
    sg = fresh(); fill(sg, P[:5])                 # QUEUING
    assert sg.request_sub(P[0])[0] is False
    assert sg.sub_for(99)[0] is False
    clock["t"] = 1000.0
    sg = fresh(); fill(sg, P)                      # PICKING (all auto-ready)
    assert sg.request_sub(P[0])[0] is True
    assert sg.sub_for(99)[0] is True
    s_live = fresh(); fill(s_live, P); cps = drive_draft(s_live)
    assert s_live.phase is Phase.LIVE
    assert s_live.request_sub(cps[0])[0] is False
    print("G) subs allowed only in PICKING (blocked in queue/ready/live)")

    # H) anyone (not just the rolled captains) can volunteer to captain,
    #    and immunity follows whoever actually captained
    clock["t"] = 1000.0
    s = fresh(); fill(s, P)
    rolled = list(s.captains)
    volunteers = [p for p in P if p not in rolled][:2]   # two NON-rolled players
    assert s.capfor(volunteers[0], "red")[0] is True
    assert s.capfor(volunteers[1], "blu")[0] is True
    pool = [p for p in P if p not in volunteers]
    while s.phase is Phase.PICKING:
        s.pick(s._current_picker(), pool.pop())
    assert s.phase is Phase.LIVE
    assert set(s.immunity) == set(volunteers), s.immunity   # volunteers got immunity
    print("H) non-rolled players can volunteer as captain; immunity follows them")

    # I) clear_auto_ready opts a player out of auto-confirm -> ready check
    clock["t"] = 1000.0
    s = fresh(); s.add(1)              # human-ish player, auto-ready 2m
    s.clear_auto_ready(1)             # opts out
    for b in range(2, 13):            # 11 more, all auto-ready
        s.add(b)
    assert s.phase is Phase.READY_CHECK, s.phase
    ready, not_ready = s.ready_status()
    assert not_ready == [1], not_ready
    print("I) clear_auto_ready -> that player is the only one in the ready check")

    # J) a captain can step down and someone else can take the slot
    clock["t"] = 1000.0
    s = fresh(); fill(s, P)
    cps = list(s.captains)
    s.capfor(cps[0], "red")
    assert s.uncap(cps[0])[0] is True
    assert cps[0] not in s.capt_of.values()
    taker = [p for p in P if p not in s.capt_of.values()][0]
    assert s.capfor(taker, "red")[0] is True
    assert s.capt_of["RED"] == taker
    assert s.uncap(999)[0] is False           # non-captain can't step down
    print("J) captain can /capoff; another player can claim the freed slot")

    # K) abort removes ONLY the aborter; other unready players stay
    clock["t"] = 1000.0
    s = fresh()
    s.add(1); s.clear_auto_ready(1)            # player 1: not auto-ready
    s.add(2); s.clear_auto_ready(2)            # player 2: not auto-ready either
    for b in range(3, 13):
        s.add(b)                               # 10 bots, auto-ready
    assert s.phase is Phase.READY_CHECK
    ok, sig, dropped = s.abort_ready_check(1)
    assert ok and dropped == [1], dropped
    assert 1 not in s.queue
    assert 2 in s.queue                         # the OTHER unready player is kept
    assert len(s.queue) == 11 and s.phase is Phase.QUEUING
    print("K) abort removes only the aborter; other unready players stay")

    # L) /match put moves a player between teams (and off the bench)
    clock["t"] = 1000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)
    assert s.phase is Phase.LIVE
    # pick a RED pick and move them to BLU
    mover = s.team["RED"][0]
    n_red, n_blu = len(s.team["RED"]), len(s.team["BLU"])
    assert s.match_put(mover, "blu")[0] is True
    assert mover in s.team["BLU"] and mover not in s.team["RED"]
    assert len(s.team["RED"]) == n_red - 1 and len(s.team["BLU"]) == n_blu + 1
    # moving a captain now works: they step down and land on the target side
    cap = caps[0]
    cap_color = next(c for c, u in s.capt_of.items() if u == cap)
    other = "BLU" if cap_color == "RED" else "RED"
    assert s.match_put(cap, other.lower())[0] is True
    assert cap not in s.capt_of.values()             # no longer a captain
    assert cap in s.team[other]                       # placed on the target team
    assert cap_color not in s.capt_of                 # old side left open
    # bench a player: removed from teams but still in the match (unpicked)
    assert s.match_put(mover, "bench")[0] is True
    assert mover not in s.team["RED"] and mover not in s.team["BLU"]
    assert mover in s.queue and mover in s.unpicked()
    print("L) /match put moves anyone incl. captains (who step down); bench works")

    # M) admin immunity management
    s = fresh()
    s.set_immunity(42, 3); assert s.immunity[42] == 3
    s.add_immunity(42, -1); assert s.immunity[42] == 2
    s.add_immunity(42, -5); assert 42 not in s.immunity     # clamps to 0 -> removed
    s.set_immunity(42, 2); s.clear_immunity(42); assert 42 not in s.immunity
    print("M) admin immunity set/add/clear works and clamps at 0")

    # N) next queue: joins during a live game wait, then promote on report
    clock["t"] = 1000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)
    assert s.phase is Phase.LIVE
    assert s.add(101)[0] is True and 101 in s.next_queue        # 13th -> next queue
    assert s.add(102)[0] is True and 102 in s.next_queue
    assert s.add(P[0])[0] is False                              # active player can't double-queue
    assert len(s.queue) == 12 and len(s.next_queue) == 2
    ok, msg, pinged = s.match_report(caps[0])                   # captain ends the game
    assert ok and len(pinged) == 12
    assert s.phase is Phase.QUEUING                             # next queue promoted
    assert set(s.queue) == {101, 102} and not s.next_queue
    print("N) next queue holds joins during a live game, promotes on report")

    # O) promoted full next queue starts a ready check immediately
    clock["t"] = 5000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)              # game LIVE, players 1..12
    for x in range(101, 113):                                   # 12 fresh players -> next queue
        s.add(x)
    assert len(s.next_queue) == 12
    ok, _, _ = s.match_report(caps[0])
    assert s.phase in (Phase.READY_CHECK, Phase.PICKING)        # promoted full -> straight into it
    assert set(s.queue) == set(range(101, 113))
    print("O) a full next queue starts its ready check on promotion")

    # P) leaving / sweeping the next queue
    clock["t"] = 1000.0
    s = fresh(); fill(s, P); drive_draft(s)
    s.add(101); s.add(102)
    assert s.remove(101)[0] is True and 101 not in s.next_queue
    clock["t"] = 1000.0 + TIMEOUT_SECONDS + 1                   # idle out the next-queue player
    dropped = s.sweep_timeouts()
    assert 102 in dropped and 102 not in s.next_queue
    print("P) next-queue players can leave and time out")

    # Q) state survives a to_dict -> JSON -> load_dict round trip (persistence)
    import json as _json
    clock["t"] = 7000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)            # LIVE, teams + immunity set
    for x in range(101, 104):
        s.add(x)                                              # 3 in the next queue
    snap = _json.loads(_json.dumps(s.to_dict()))              # force JSON string keys
    s2 = PugState(now=lambda: clock["t"])
    s2.load_dict(snap)
    assert s2.phase is s.phase
    assert s2.queue_ids() == s.queue_ids()
    assert s2.next_queue_ids() == s.next_queue_ids()
    assert s2.team == s.team and s2.capt_of == s.capt_of
    assert s2.immunity == s.immunity
    assert s2.turn_idx == s.turn_idx and s2.picks_left == s.picks_left
    ok, _, pinged = s2.match_report(caps[0])                  # restored captain can still report
    assert ok and set(s2.queue) == {101, 102, 103}            # next queue promoted after reload
    print("Q) full state round-trips through JSON and stays functional")

    # R) /clear during a game wipes only the next queue, not the live match
    clock["t"] = 9000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)           # LIVE
    for x in (201, 202): s.add(x)                            # next queue
    live_players = s.queue_ids()
    ok, _ = s.admin_clear()
    assert ok
    assert s.phase is Phase.LIVE                              # game untouched
    assert s.queue_ids() == live_players                      # same 12 still playing
    assert s.next_queue_ids() == []                           # next queue wiped
    # with no game running, /clear wipes the active queue as before
    s.match_report(caps[0])                                   # ends game; next queue empty -> IDLE
    s.add(301); s.add(302)
    assert s.phase is Phase.QUEUING
    s.admin_clear()
    assert s.phase is Phase.IDLE and s.queue_ids() == []
    print("R) /clear scopes to the next queue mid-game, full queue otherwise")

    # S) the forced last unpicked player is auto-assigned (draft ends a pick early)
    clock["t"] = 11000.0
    s = fresh(); fill(s, P)
    caps = list(s.captains)
    s.capfor(caps[0], "red"); s.capfor(caps[1], "blu")
    while len(s.unpicked()) > 1 and s.phase is Phase.PICKING:
        s.pick(s._current_picker(), s.unpicked()[0])
    assert s.phase is Phase.LIVE                       # auto-assign finished it
    assert s.unpicked() == []                          # nobody left hanging
    assert sorted(s.team["RED"] + s.team["BLU"] + list(s.capt_of.values())) == P
    assert len(s.team["RED"]) == 5 and len(s.team["BLU"]) == 5
    print("S) the forced last player is auto-assigned; draft finishes one pick early")

    # T) arming auto-ready during a ready check confirms the player
    clock["t"] = 12000.0
    s = fresh()
    for p in range(1, 12):
        s.add(p)                                       # 1..11 auto-ready, QUEUING
    s.clear_auto_ready(10); s.clear_auto_ready(11)     # two opt out of auto-ready
    s.add(12)                                          # fills -> ready check (not auto-draft)
    assert s.phase is Phase.READY_CHECK
    assert 10 not in s.ready and 11 not in s.ready and 12 in s.ready
    ok, _ = s.add(10, AR_COMMAND_SECONDS)              # 10 arms AR mid-check
    assert ok and 10 in s.ready                        # confirmed now
    assert s.auto_ready_until[10] > NOW()              # and armed for a restart
    assert s.phase is Phase.READY_CHECK                # 11 still not ready -> check continues
    ok, _ = s.add(11, AR_COMMAND_SECONDS)              # last one arms AR
    assert ok and s.phase is Phase.PICKING             # completes the check -> draft
    print("T) arming auto-ready during a ready check confirms the player (and completes it when last)")

    # U) a live game auto-ends after the report window (captains forgot)
    clock["t"] = 13000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)        # LIVE
    for x in (201, 202): s.add(x)                          # next queue waiting
    assert s.check_live_timeout() is None                  # not time yet
    clock["t"] = 13000.0 + LIVE_AUTO_REPORT_SECONDS - 1
    assert s.check_live_timeout() is None                  # still under the limit
    clock["t"] = 13000.0 + LIVE_AUTO_REPORT_SECONDS + 1
    finished = s.check_live_timeout()
    assert finished is not None and len(finished) == 12    # the 12 who were playing
    assert set(s.queue) == {201, 202}                       # next queue promoted
    assert s.live_since is None                             # clock reset
    assert s.check_live_timeout() is None                   # idempotent (no longer LIVE)
    print("U) live games auto-report after the time limit and promote the next queue")

    # V) failed ready check never overflows the active queue past 12.
    #    12 queued + 6 waiting in next queue; 2 fail to ready. The 2 are dropped,
    #    exactly 2 are pulled from the next queue to refill to 12, and the others
    #    stay waiting — the draft must NOT start with 16.
    clock["t"] = 1000.0
    s = fresh()
    s.add(1); s.clear_auto_ready(1)            # two players who won't auto-confirm
    s.add(2); s.clear_auto_ready(2)
    for b in range(3, 13):                      # 10 more -> queue is full (12), check fires
        s.add(b)
    assert s.phase is Phase.READY_CHECK and len(s.queue) == 12
    for x in range(101, 107):                   # 6 waiters land in the next queue
        clock["t"] = 1000.0 + (x - 100)         # staggered joins so order is well-defined
        s.add(x)
    assert len(s.next_queue) == 6
    s.mark_ready(2)                             # only player 1 stays unready
    dropped = s.resolve_ready_check()           # window expires
    assert dropped == [1], dropped
    assert len(s.queue) == 12, len(s.queue)     # refilled to exactly 12, NOT 16
    assert len(s.next_queue) == 5               # 1 promoted to backfill, 5 still waiting
    assert 101 in s.queue                       # oldest waiter promoted first
    assert {102, 103, 104, 105, 106} == set(s.next_queue)
    assert s.phase in (Phase.READY_CHECK, Phase.PICKING)  # a fresh check on the 12
    print("V) failed ready check refills to 12 and never overflows the queue")

    print("\nall smoke tests passed.")

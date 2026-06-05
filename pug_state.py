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
READY_CHECK_SECONDS = 60         # window to ready up once queue is full

# RED first. (team, picks_this_turn). 1-2-1-1-1-1-1-1-1 -> 5/5 picks = 6v6.
PICK_ORDER = [
    ("RED", 1), ("BLU", 2), ("RED", 1), ("BLU", 1), ("RED", 1),
    ("BLU", 1), ("RED", 1), ("BLU", 1), ("RED", 1),
]


class PugState:
    def __init__(self, now=time.time):
        self._now = now
        self.immunity = {}            # uid -> med-immunity games left (spans games)
        self.auto_ready_until = {}    # uid -> epoch its auto-ready window ends
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

    def reset_all(self):
        self.immunity = {}
        self.auto_ready_until = {}
        self.reset_match()

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

    def unpicked(self):
        drafted = self._drafted()
        return [u for u in self.queue if u not in drafted]

    # ---------- queue + auto-ready ----------
    def add(self, uid, ar_seconds=DEFAULT_AR_SECONDS):
        """Join (or re-arm auto-ready if already queued)."""
        if self.phase not in (Phase.IDLE, Phase.QUEUING):
            return False, "A game is in progress. Wait for it to finish."
        ar_seconds = min(ar_seconds, MAX_AR_SECONDS)
        self.auto_ready_until[uid] = self._now() + ar_seconds
        if uid in self.queue:
            return True, f"Auto-ready extended. ({len(self.queue)}/{QUEUE_SIZE})"
        self.phase = Phase.QUEUING
        self.queue[uid] = self._now()
        if len(self.queue) >= QUEUE_SIZE:
            return self._begin_ready_check()
        return True, f"Queued ({len(self.queue)}/{QUEUE_SIZE})."

    def clear_auto_ready(self, uid):
        """Turn off a player's auto-confirm; they must ready up manually."""
        if self.phase != Phase.QUEUING:
            return False, "Only works while queuing."
        if uid not in self.queue:
            return False, "You're not in the queue."
        self.auto_ready_until[uid] = 0
        return True, "Auto-ready off — you'll need to ready up manually."

    def remove(self, uid):
        if self.phase != Phase.QUEUING:
            return False, "Can only leave while queuing."
        if uid not in self.queue:
            return False, "You're not in the queue."
        del self.queue[uid]
        if not self.queue:
            self.phase = Phase.IDLE
        return True, "Left the queue."

    def sweep_timeouts(self):
        """Idle-drop sweep (30 min). Background loop. QUEUING only."""
        if self.phase != Phase.QUEUING:
            return []
        now, dropped = self._now(), []
        for uid, joined in list(self.queue.items()):
            if now - joined >= TIMEOUT_SECONDS:
                del self.queue[uid]
                dropped.append(uid)
        if not self.queue:
            self.phase = Phase.IDLE
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

    def abort_ready_check(self, uid):
        """A participant bails during the ready check. ONLY they leave the
        queue; everyone else stays. Since the queue is no longer full, we drop
        back to QUEUING (a fresh check fires when it refills).
        Returns (ok, msg, dropped)."""
        if self.phase != Phase.READY_CHECK:
            return False, "No ready check active.", []
        if uid not in self.queue:
            return False, "You're not in this match.", []
        del self.queue[uid]
        self.ready = set()
        self.phase = Phase.QUEUING if self.queue else Phase.IDLE
        return True, "aborted", [uid]

    def resolve_ready_check(self):
        """Called when the ready window expires. Drops the not-ready; returns them."""
        if self.phase != Phase.READY_CHECK:
            return []
        not_ready = [u for u in self.queue if u not in self.ready]
        for u in not_ready:
            del self.queue[u]
        self.ready = set()
        self.phase = Phase.QUEUING if self.queue else Phase.IDLE
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
        self.team[self._current_team()].append(target)
        self.picks_left -= 1
        if self.picks_left == 0:
            self.turn_idx += 1
            if self.turn_idx >= len(PICK_ORDER):
                return self._go_live()
            self.picks_left = PICK_ORDER[self.turn_idx][1]
        return True, f"<@{target}> -> {self._current_team()}. Next: <@{self._current_picker()}>."

    def _go_live(self):
        self._apply_immunity()
        self.phase = Phase.LIVE
        return True, "Teams set. GLHF!"

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
            self.reset_match()
            return True, "Game reported. Queue open — /add to join.", players
        if self.phase in (Phase.READY_CHECK, Phase.PICKING):
            if not is_admin:
                return False, "Only admins can cancel a forming match.", []
            players = list(self.queue.keys())
            self.reset_match()
            return True, "your match has been canceled.", players
        return False, "No match to report.", []

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
        self.reset_match()
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

    print("\nall smoke tests passed.")
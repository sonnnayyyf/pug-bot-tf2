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
ELO_START = 1600                 # everyone's starting rating (cosmetic; never gates play)
ELO_K = 32                       # rating responsiveness (per-player, before clamping)
ELO_CLAMP = 20                   # hard cap on how much one game can move a player (±)

# RED first. (team, picks_this_turn). 1-1-1-2-1-1-1-1-1 -> 5/5 picks = 6v6.
# BLU's double pick sits mid-draft (its 2nd turn) instead of right after RED's
# first-overall pick, so the first-pick compensation is spread out / fairer.
PICK_ORDER = [
    ("RED", 1), ("BLU", 1), ("RED", 1), ("BLU", 2), ("RED", 1),
    ("BLU", 1), ("RED", 1), ("BLU", 1), ("RED", 1),
]


class PugState:
    def __init__(self, now=time.time, immunity=None, stats=None):
        self._now = now
        # immunity may be a dict shared across several PugStates (multi-lobby:
        # immunity is a property of the player, not the channel). We only ever
        # mutate it in place so the shared reference stays intact.
        self.immunity = immunity if immunity is not None else {}
        # lifetime per-player counters, a shared-across-lobbies dict:
        #   {uid: {"games", "capt", "w", "l", "elo"}}
        # games/capt are bumped at go-live; w/l/elo are recorded at report time
        # (only a game with a declared winner counts). Old snapshots that predate
        # the w/l/elo keys upgrade transparently (see _stat_rec). Persisted by the
        # bot layer; the engine never reads it back to make decisions (cosmetic).
        self.stats = stats if stats is not None else {}
        # Transient hand-off: set by match_report to the detail of the game just
        # recorded (winner, rosters, per-player before/after Elo) so the bot can
        # write it to the audit log. NOT persisted and NOT part of to_dict — it's
        # read once, immediately after the report call, then ignored.
        self.last_result = None
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
        self.live_since = None        # epoch the game went LIVE

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

    def _draft_full(self):
        """True when both captains are set and every slot is filled (6v6) — the
        condition for a draft to go live, no matter how it got filled (normal
        /pick order or manual /match put)."""
        half = QUEUE_SIZE // 2
        return (self._both_capts_set and not self.unpicked()
                and len(self._side_players("RED")) == half
                and len(self._side_players("BLU")) == half)

    def _settle_draft(self):
        """After a manual team change during PICKING (e.g. /match put), assign a
        forced last player when only one slot is open, then go live once the teams
        are full. No-op outside PICKING, with a missing captain, or mid-draft —
        so a partial change just leaves the draft running."""
        if self.phase is not Phase.PICKING or not self._both_capts_set:
            return
        half = QUEUE_SIZE // 2
        rem = self.unpicked()
        if len(rem) == 1:                       # exactly one open slot -> forced
            if len(self._side_players("RED")) < half:
                self.team["RED"].append(rem[0])
            elif len(self._side_players("BLU")) < half:
                self.team["BLU"].append(rem[0])
        if self._draft_full():
            self._go_live()

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
        self._bump_stats()
        self.phase = Phase.LIVE
        self.live_since = self._now()
        return True, "Teams set. GLHF!"

    def _stat_rec(self, uid):
        """The lifetime record for `uid`, created — and back-filled — on demand.
        Centralising the default shape here means a snapshot saved before the
        w/l/elo keys existed upgrades transparently the first time it's touched,
        so no DB migration or wipe is needed."""
        rec = self.stats.setdefault(uid, {})
        rec.setdefault("games", 0)
        rec.setdefault("capt", 0)
        rec.setdefault("w", 0)
        rec.setdefault("l", 0)
        rec.setdefault("d", 0)
        rec.setdefault("elo", ELO_START)
        return rec

    def _bump_stats(self):
        """Count one game played for everyone on the final roster, and one
        captain appearance for each captain. Called exactly once, at go-live.
        Win/loss and Elo are NOT touched here — the winner isn't known until a
        game is reported (see _record_result)."""
        for p in self._all_players():
            self._stat_rec(p)["games"] += 1
        for c in set(self.capt_of.values()):
            self._stat_rec(c)["capt"] += 1

    def _side_players(self, color):
        """Everyone on one side: that color's captain (the medic) plus their
        picks. Captains live in capt_of, picks in team[color], so there's no
        overlap; the membership guard is just belt-and-suspenders."""
        members = list(self.team[color])
        cap = self.capt_of.get(color)
        if cap is not None and cap not in members:
            members.insert(0, cap)
        return members

    def _avg_elo(self, players):
        if not players:
            return ELO_START
        return sum(self._stat_rec(p)["elo"] for p in players) / len(players)

    def _compute_deltas(self, red, blu, before, winner):
        """Pure: given rosters, each player's before-rating, and the result
        ('RED'/'BLU'/'DRAW'), return {uid: integer delta} after the per-player
        expectation, zero-sum renormalization, and ±ELO_CLAMP clamp. No state
        is read or written, so it's reusable for both live results and after-the-
        fact corrections."""
        avg_red = sum(before[p] for p in red) / len(red) if red else ELO_START
        avg_blu = sum(before[p] for p in blu) / len(blu) if blu else ELO_START

        def expected(rating, opp_avg):
            return 1.0 / (1.0 + 10 ** ((opp_avg - rating) / 400.0))

        def actual(side):
            if winner == "DRAW":
                return 0.5
            return 1.0 if winner == side else 0.0

        raw = {}
        for p in red:
            raw[p] = ELO_K * (actual("RED") - expected(before[p], avg_blu))
        for p in blu:
            raw[p] = ELO_K * (actual("BLU") - expected(before[p], avg_red))

        gainers = [p for p in raw if raw[p] > 0]
        losers = [p for p in raw if raw[p] < 0]
        pos = sum(raw[p] for p in gainers)
        neg = -sum(raw[p] for p in losers)
        target = (pos + neg) / 2.0
        if pos > 0:
            for p in gainers:
                raw[p] *= target / pos
        if neg > 0:
            for p in losers:
                raw[p] *= target / neg
        return {p: round(max(-ELO_CLAMP, min(ELO_CLAMP, raw[p]))) for p in raw}

    def _apply_record_counts(self, red, blu, winner, sign):
        """Add `sign` (+1 to record, -1 to undo) to the W/L/D counters for one
        result. Counters never go below zero."""
        def bump(uid, key):
            rec = self._stat_rec(uid)
            rec[key] = max(0, rec[key] + sign)
        if winner == "DRAW":
            for p in red + blu:
                bump(p, "d")
        else:
            win_side, lose_side = (red, blu) if winner == "RED" else (blu, red)
            for p in win_side:
                bump(p, "w")
            for p in lose_side:
                bump(p, "l")

    @staticmethod
    def _swing(deltas):
        moved = [abs(v) for v in deltas.values() if v != 0]
        return round(sum(moved) / len(moved)) if moved else 0

    def _record_result(self, winner):
        """Apply one reported outcome ('RED'/'BLU'/'DRAW') and return a detail
        record for the audit log: winner, both rosters, a representative swing,
        and each player's [before, after] Elo."""
        red, blu = self._side_players("RED"), self._side_players("BLU")
        before = {p: self._stat_rec(p)["elo"] for p in red + blu}
        delta = self._compute_deltas(red, blu, before, winner)
        for p in red + blu:
            self._stat_rec(p)["elo"] = before[p] + delta[p]
        self._apply_record_counts(red, blu, winner, +1)
        return {
            "winner": winner,
            "delta": self._swing(delta),
            "red": list(red),
            "blu": list(blu),
            "elos": {p: [before[p], self._stat_rec(p)["elo"]] for p in red + blu},
        }

    def correct_match(self, red, blu, before, old_winner, new_winner):
        """Re-grade a previously-recorded match: undo the old result and apply the
        new one, using the ratings as they were AT MATCH TIME (from the audit
        log), so admins never hand-compute corrections. Adjusts each player's
        current Elo by the difference and fixes their W/L/D. Returns the corrected
        detail dict for re-logging."""
        old = self._compute_deltas(red, blu, before, old_winner)
        new = self._compute_deltas(red, blu, before, new_winner)
        for p in red + blu:
            self._stat_rec(p)["elo"] += new.get(p, 0) - old.get(p, 0)
        self._apply_record_counts(red, blu, old_winner, -1)
        self._apply_record_counts(red, blu, new_winner, +1)
        return {
            "winner": new_winner,
            "delta": self._swing(new),
            "red": list(red),
            "blu": list(blu),
            "elos": {p: [before[p], before[p] + new.get(p, 0)] for p in red + blu},
            "corrected_from": old_winner,
        }

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
    def match_report(self, uid, is_admin=False, winner=None):
        """Record a finished LIVE game's result (W/L/D + Elo) and reopen the queue.
        Valid only while LIVE, and an outcome ('RED'/'BLU'/'DRAW') is REQUIRED — a
        captain can't end a game without naming the result. A captain or an admin
        may report. To end a game with no result, use match_cancel (admin only).
        Returns (ok, msg, players_to_ping)."""
        self.last_result = None
        if self.phase != Phase.LIVE:
            return False, "No live game to report.", []
        if not is_admin and uid not in self.capt_of.values():
            return False, "Only a captain or admin can report.", []
        w = (winner or "").upper() or None
        if w not in ("RED", "BLU", "DRAW"):
            return False, "Say who won: /match report red, blu, or draw.", []
        players = list(self.queue.keys())
        self.last_result = self._record_result(w)
        self._end_and_promote()
        if w == "DRAW":
            return True, "Draw — recorded. Queue open — /add to join.", players
        return True, f"{w} wins — recorded. Queue open — /add to join.", players

    def match_cancel(self, uid, is_admin=False):
        """Admin: end a match with NO result recorded — void a live game that was
        abandoned/never really played, or scrap a forming match. Touches nobody's
        Elo or W/L. Returns (ok, msg, players_to_ping)."""
        self.last_result = None
        if not is_admin:
            return False, "Only admins can cancel a match.", []
        if self.phase not in (Phase.LIVE, Phase.READY_CHECK, Phase.PICKING):
            return False, "No match to cancel.", []
        was_live = self.phase is Phase.LIVE
        players = list(self.queue.keys())
        self._end_and_promote()
        msg = ("Game voided — no result recorded. Queue open — /add to join."
               if was_live else "your match has been canceled.")
        return True, msg, players

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
        """Admin: move a player onto a team, into a captain slot, or to the bench.
        Used to rebalance. Moving a player out of a captain slot frees it; moving
        one INTO a captain slot bumps the previous captain there to unpicked."""
        if self.phase not in (Phase.PICKING, Phase.LIVE):
            return False, "No teams to move players between yet."
        where = where.upper()
        if where not in ("RED", "BLU", "BENCH", "CAPT_RED", "CAPT_BLU"):
            return False, "Target must be red, blu, a captain slot, or bench."
        if uid not in self.queue:
            return False, "That player isn't in this match."
        was_capt = None
        for color, c in list(self.capt_of.items()):     # detach from any captaincy
            if c == uid:
                was_capt = color
                del self.capt_of[color]
        for c in ("RED", "BLU"):                          # detach from team picks
            if uid in self.team[c]:
                self.team[c].remove(uid)
        note = f" (was {was_capt} captain)" if was_capt else ""
        if where in ("CAPT_RED", "CAPT_BLU"):
            color = "RED" if where == "CAPT_RED" else "BLU"
            displaced = self.capt_of.get(color)
            self.capt_of[color] = uid
            dnote = (f" <@{displaced}> is now unpicked." if displaced and displaced != uid
                     else "")
            msg = f"Made <@{uid}> {color} captain{note}.{dnote}"
        elif where == "BENCH":
            msg = f"Moved <@{uid}> to the bench{note}."
        else:
            if uid not in self.team[where]:
                self.team[where].append(uid)
            msg = f"Moved <@{uid}> to {where}{note}."
        # a /match put can complete the draft; if the teams are now full, go live
        if self.phase is Phase.PICKING:
            self._settle_draft()
            if self.phase is Phase.LIVE:
                msg += " Teams full — match is now live. GLHF!"
        return True, msg

    def match_start(self, uid, is_admin=False):
        """Admin: force a fully-set PICKING draft to go live — e.g. after the teams
        were arranged by hand with /match put. Only works once both teams are full
        6v6; otherwise it says what's still missing."""
        if not is_admin:
            return False, "Only admins can start the match."
        if self.phase is not Phase.PICKING:
            return False, "No draft waiting to start."
        self._settle_draft()
        if self.phase is Phase.LIVE:
            return True, "Teams set — match is now live. GLHF!"
        half = QUEUE_SIZE // 2
        return False, (f"Teams aren't full yet — RED {len(self._side_players('RED'))}/{half}, "
                       f"BLU {len(self._side_players('BLU'))}/{half}. Fill them with /match put first.")

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

    # ---------- Elo admin ----------
    def set_elo(self, uid, value):
        """Admin: set a player's Elo to an exact value. Returns
        (ok, msg, before, after) so the caller can write it to the audit log."""
        rec = self._stat_rec(uid)
        before = rec["elo"]
        rec["elo"] = int(value)
        return True, f"<@{uid}> Elo set to {rec['elo']} (was {before}).", before, rec["elo"]

    def add_elo(self, uid, delta):
        """Admin: nudge a player's Elo by a (possibly negative) amount. Returns
        (ok, msg, before, after)."""
        rec = self._stat_rec(uid)
        before = rec["elo"]
        rec["elo"] = before + int(delta)
        sign = "+" if int(delta) >= 0 else ""
        return True, f"<@{uid}> Elo {sign}{int(delta)} → {rec['elo']} (was {before}).", before, rec["elo"]

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
    ok, msg, pinged = s.match_report(caps[0], winner="red")
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
    ok, _, _ = s.match_cancel(99999, is_admin=False)
    assert ok is False
    ok, msg, pinged = s.match_cancel(99999, is_admin=True)
    assert ok and "canceled" in msg and len(pinged) == 12 and s.phase is Phase.IDLE
    print("D) admin /match cancel scraps a forming match; non-admin blocked")

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
    ok, msg, pinged = s.match_report(caps[0], winner="red")    # captain ends the game
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
    ok, _, _ = s.match_report(caps[0], winner="red")
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
    ok, _, pinged = s2.match_report(caps[0], winner="red")    # restored captain can still report
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
    s.match_report(caps[0], winner="red")                     # ends game; next queue empty -> IDLE
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

    # U) a live game never auto-ends — it stays LIVE until a human reports.
    #    (The old 50-min auto-void was removed; only a report/cancel ends a game.)
    clock["t"] = 13000.0
    s = fresh(); fill(s, P); caps = drive_draft(s)        # LIVE
    assert not hasattr(s, "check_live_timeout")            # the auto-end is gone
    clock["t"] = 13000.0 + 10 * 60 * 60                    # 10 hours later
    s.sweep_timeouts()                                     # the only remaining background sweep
    assert s.phase is Phase.LIVE                            # still live — no timer can end it
    assert set(s.queue) == set(P)
    ok, _, _ = s.match_report(caps[0], winner="red")        # a real report is the only way out
    assert ok and s.phase is Phase.IDLE
    print("U) a live game never auto-ends; only a captain/admin report ends it")

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

    # W) stats: a completed game bumps games for all 12 and capt for the 2 captains;
    #    a shared stats dict accumulates across multiple PugStates (lobbies).
    clock["t"] = 1000.0
    shared_stats = {}
    s = PugState(now=NOW, stats=shared_stats); fill(s, P)
    caps = drive_draft(s)
    assert s.phase is Phase.LIVE
    assert all(shared_stats[p]["games"] == 1 for p in P), shared_stats
    assert shared_stats[caps[0]]["capt"] == 1 and shared_stats[caps[1]]["capt"] == 1
    assert sum(r["capt"] for r in shared_stats.values()) == 2   # exactly 2 captains counted
    s.match_report(caps[0], winner="red")
    # a SECOND lobby sharing the same dict adds to the same totals
    s2 = PugState(now=NOW, stats=shared_stats); fill(s2, P)
    caps2 = drive_draft(s2)
    assert all(shared_stats[p]["games"] == 2 for p in P), "games should accumulate to 2"
    # whoever captained in both games now shows capt == 2
    twice = [c for c in caps if c in caps2]
    assert all(shared_stats[c]["capt"] == 2 for c in twice)
    print("W) stats count games + captaincies and accumulate across shared lobbies")

    # X) match_put can drop a player into a captain slot, bumping the old captain
    clock["t"] = 1000.0
    s = fresh(); fill(s, P)
    caps = drive_draft(s)                         # LIVE, both captains set
    old_red = s.capt_of["RED"]
    victim = next(u for u in P if u not in s.capt_of.values())   # some non-captain
    ok, msg = s.match_put(victim, "capt_red")
    assert ok and s.capt_of["RED"] == victim, (msg, s.capt_of)
    assert old_red != victim and old_red in s.unpicked()         # old captain bumped to unpicked
    assert victim not in s.team["RED"] and victim not in s.team["BLU"]
    # putting a current captain onto a team frees their slot
    blu_cap = s.capt_of["BLU"]
    ok, msg = s.match_put(blu_cap, "red")
    assert ok and "BLU" not in s.capt_of and blu_cap in s.team["RED"], (msg, s.capt_of)
    print("X) match_put assigns/bumps captains and frees slots")

    # Y) reporting a winner records W/L + Elo (zero-sum); a captain must name a
    #    winner to report.
    clock["t"] = 1000.0
    st = {}
    s = PugState(now=NOW, stats=st); fill(s, P)
    caps = drive_draft(s)                              # LIVE, all 12 at base Elo
    red, blu = s._side_players("RED"), s._side_players("BLU")
    assert len(red) == 6 and len(blu) == 6 and not (set(red) & set(blu))
    ok, _, _ = s.match_report(caps[0])                 # captain, no winner -> refused
    assert ok is False and s.phase is Phase.LIVE
    ok, msg, players = s.match_report(caps[0], winner="red")
    assert ok and len(players) == 12
    for p in red:
        assert st[p]["w"] == 1 and st[p]["l"] == 0 and st[p]["elo"] == ELO_START + 16
    for p in blu:
        assert st[p]["l"] == 1 and st[p]["w"] == 0 and st[p]["elo"] == ELO_START - 16
    assert sum(st[p]["elo"] for p in P) == ELO_START * 12        # Elo is zero-sum
    print("Y) a declared winner records W/L + zero-sum Elo; captain must name one")

    # Y2) an admin can void a live game (match_cancel) — ends it, records NOTHING,
    #     though the game still counts as 'played' (bumped at go-live).
    clock["t"] = 2000.0
    st2 = {}
    s = PugState(now=NOW, stats=st2); fill(s, P)
    drive_draft(s)
    assert s.phase is Phase.LIVE
    ok, msg, players = s.match_cancel(0, is_admin=True)          # admin void of a live game
    assert ok and "void" in msg.lower() and len(players) == 12
    assert all(st2[p]["w"] == 0 and st2[p]["l"] == 0 for p in P)
    assert all(st2[p]["elo"] == ELO_START for p in P)
    assert all(st2[p]["games"] == 1 for p in P)                  # void still 'played'
    print("Y2) an admin void (match_cancel) ends the game and records no W/L or Elo")

    # Z) records saved before the w/l/elo keys existed upgrade in place the first
    #    time they're touched — so an existing pug.db needs no migration or wipe.
    st_old = {7: {"games": 5, "capt": 2}}                        # legacy record shape
    s = PugState(now=NOW, stats=st_old)
    rec = s._stat_rec(7)
    assert rec["games"] == 5 and rec["capt"] == 2                # history preserved
    assert rec["w"] == 0 and rec["l"] == 0 and rec["d"] == 0 and rec["elo"] == ELO_START  # back-filled
    print("Z) legacy stat records upgrade in place (no DB wipe needed)")

    # AA) match_report exposes a detail record (for the audit log) on a recorded
    #     game, leaves it None on a void, and never leaks into the snapshot;
    #     admin set/add Elo report before/after for logging.
    clock["t"] = 1000.0
    st = {}
    s = PugState(now=NOW, stats=st); fill(s, P)
    caps = drive_draft(s)
    red, blu = s._side_players("RED"), s._side_players("BLU")
    s.match_report(caps[0], winner="blu")
    det = s.last_result
    assert det is not None
    assert det["winner"] == "BLU" and det["delta"] == 16
    assert set(det["red"]) == set(red) and set(det["blu"]) == set(blu)
    assert len(det["elos"]) == 12
    # every blu player's record shows [1000, 1016]; red [1000, 984]
    assert all(det["elos"][p] == [ELO_START, ELO_START + 16] for p in blu)
    assert all(det["elos"][p] == [ELO_START, ELO_START - 16] for p in red)
    assert "last_result" not in s.to_dict()                     # transient, never persisted
    # a void leaves no detail
    clock["t"] = 2000.0
    s2 = PugState(now=NOW, stats={}); fill(s2, P); drive_draft(s2)
    s2.match_cancel(0, is_admin=True)                           # void
    assert s2.last_result is None
    # admin Elo edits report before/after
    ok, msg, before, after = s.set_elo(blu[0], 1234)
    assert ok and before == ELO_START + 16 and after == 1234
    ok, msg, before, after = s.add_elo(blu[0], -34)
    assert ok and before == 1234 and after == 1200
    print("AA) report yields an audit detail (None on void); admin set/add Elo report before/after")

    # BB) per-player Elo: within a team, a higher-rated player moves less than a
    #     lower-rated one; the result renormalizes to ~zero-sum; all moves clamp ±20.
    st = {}
    s = PugState(now=NOW, stats=st)
    ratings = {1: 1600, 2: 1600, 3: 1600,      # RED (will lose)
               4: 1500, 5: 1600, 6: 1700}      # BLU (will win) — spread within the team
    for u, r in ratings.items():
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "elo": r}
    s.phase = Phase.LIVE
    s.capt_of = {"RED": 1, "BLU": 4}
    s.team = {"RED": [2, 3], "BLU": [5, 6]}
    s.queue = {u: 0 for u in ratings}
    det = s._record_result("BLU")                                # BLU wins
    gain = {u: st[u]["elo"] - ratings[u] for u in (4, 5, 6)}
    # within the winning team, the lowest-rated (4=1500) gains most, the highest (6) least
    assert gain[4] > gain[5] > gain[6], gain
    # every move is within the clamp
    assert all(abs(st[u]["elo"] - ratings[u]) <= ELO_CLAMP for u in ratings)
    # ~zero-sum across the match (within integer rounding)
    won = sum(st[u]["elo"] - ratings[u] for u in (4, 5, 6))
    lost = sum(ratings[u] - st[u]["elo"] for u in (1, 2, 3))
    assert abs(won - lost) <= 2, (won, lost)
    assert all(st[u]["w"] == 1 for u in (4, 5, 6)) and all(st[u]["l"] == 1 for u in (1, 2, 3))
    print("BB) per-player Elo varies by own rating, renormalizes ~zero-sum, clamps ±20")

    # BB2) a lopsided result is held to the clamp even when raw deltas blow past it
    st = {}
    s = PugState(now=NOW, stats=st)
    for u in (1, 2, 3):
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "elo": 1900}   # strong RED
    for u in (4, 5, 6):
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "elo": 1300}   # weak BLU
    s.phase = Phase.LIVE
    s.capt_of = {"RED": 1, "BLU": 4}
    s.team = {"RED": [2, 3], "BLU": [5, 6]}
    s.queue = {u: 0 for u in range(1, 7)}
    s._record_result("BLU")                                      # big upset
    assert all(st[u]["elo"] == 1300 + ELO_CLAMP for u in (4, 5, 6))   # +20 exactly
    assert all(st[u]["elo"] == 1900 - ELO_CLAMP for u in (1, 2, 3))   # -20 exactly
    print("BB2) a blowout upset is capped at exactly ±20")

    # CC) filling teams with /match put completes the draft and goes live (the bug
    #     was that manual puts never tripped the go-live transition).
    clock["t"] = 1000.0
    s = fresh(); fill(s, P)
    assert s.phase is Phase.PICKING
    caps = list(s.captains)
    s.capfor(caps[0], "red"); s.capfor(caps[1], "blu")
    others = [u for u in P if u not in caps]
    for i, u in enumerate(others):
        ok, _ = s.match_put(u, "red" if i % 2 == 0 else "blu")
        assert ok
    half = QUEUE_SIZE // 2
    assert s.phase is Phase.LIVE, (s.phase, len(s.unpicked()))
    assert len(s._side_players("RED")) == half and len(s._side_players("BLU")) == half
    print("CC) filling teams with /match put completes the draft and goes live")

    # CC2) /match start force-lives a full draft that's somehow still in PICKING
    #      (admin only), and refuses an unfilled one.
    s = fresh(); fill(s, P)
    caps = list(s.captains)
    s.capfor(caps[0], "red"); s.capfor(caps[1], "blu")
    others = [u for u in P if u not in caps]
    for i, u in enumerate(others):                  # hand-place all 10 WITHOUT settling
        (s.team["RED"] if i < 5 else s.team["BLU"]).append(u)
    s.phase = Phase.PICKING                          # simulate the stuck state
    assert not s.unpicked()
    assert s.match_start(0, is_admin=False)[0] is False and s.phase is Phase.PICKING   # non-admin
    ok, msg = s.match_start(0, is_admin=True)
    assert ok and s.phase is Phase.LIVE
    # an unfilled draft is refused
    s = fresh(); fill(s, P)
    caps = list(s.captains)
    s.capfor(caps[0], "red"); s.capfor(caps[1], "blu")
    s.match_put(next(u for u in P if u not in caps), "red")   # one pick only
    ok, msg = s.match_start(0, is_admin=True)
    assert ok is False and "full" in msg.lower() and s.phase is Phase.PICKING
    print("CC2) /match start force-lives a stuck full draft (admin only); refuses a partial one")

    # DD) a draw: balanced teams don't move; unbalanced teams converge; everyone
    #     gets a draw, nobody a W or L.
    st = {}
    s = PugState(now=NOW, stats=st)
    for u in range(1, 7):
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "d": 0, "elo": 1600}
    s.phase = Phase.LIVE
    s.capt_of = {"RED": 1, "BLU": 4}
    s.team = {"RED": [2, 3], "BLU": [5, 6]}
    s.queue = {u: 0 for u in range(1, 7)}
    det = s._record_result("DRAW")
    assert det["winner"] == "DRAW"
    assert all(st[u]["elo"] == 1600 for u in range(1, 7))         # balanced -> no movement
    assert all(st[u]["d"] == 1 and st[u]["w"] == 0 and st[u]["l"] == 0 for u in range(1, 7))

    st = {}
    s = PugState(now=NOW, stats=st)
    for u in (1, 2, 3):
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "d": 0, "elo": 1800}   # strong RED
    for u in (4, 5, 6):
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "d": 0, "elo": 1400}   # weak BLU
    s.phase = Phase.LIVE
    s.capt_of = {"RED": 1, "BLU": 4}
    s.team = {"RED": [2, 3], "BLU": [5, 6]}
    s.queue = {u: 0 for u in range(1, 7)}
    s._record_result("DRAW")
    assert all(st[u]["elo"] < 1800 for u in (1, 2, 3))            # favorite drops
    assert all(st[u]["elo"] > 1400 for u in (4, 5, 6))            # underdog rises
    dn = sum(1800 - st[u]["elo"] for u in (1, 2, 3))
    up = sum(st[u]["elo"] - 1400 for u in (4, 5, 6))
    assert abs(dn - up) <= 2                                      # ~zero-sum
    assert all(abs(st[u]["elo"] - (1800 if u < 4 else 1400)) <= ELO_CLAMP for u in range(1, 7))
    assert all(st[u]["d"] == 1 and st[u]["w"] == 0 and st[u]["l"] == 0 for u in range(1, 7))
    print("DD) a draw: balanced no-move, unbalanced converges ~zero-sum, all +1 D no W/L")

    # EE) correcting a misreported match flips Elo + W/L using match-time ratings.
    st = {}
    s = PugState(now=NOW, stats=st)
    for u in range(1, 7):
        st[u] = {"games": 1, "capt": 0, "w": 0, "l": 0, "d": 0, "elo": 1600}
    s.phase = Phase.LIVE
    s.capt_of = {"RED": 1, "BLU": 4}
    s.team = {"RED": [2, 3], "BLU": [5, 6]}
    s.queue = {u: 0 for u in range(1, 7)}
    det = s._record_result("BLU")                    # WRONG: reported BLU win
    red, blu = det["red"], det["blu"]
    before = {p: det["elos"][p][0] for p in red + blu}
    assert all(st[u]["elo"] == 1616 for u in blu) and all(st[u]["elo"] == 1584 for u in red)
    assert all(st[u]["w"] == 1 for u in blu) and all(st[u]["l"] == 1 for u in red)
    s.correct_match(red, blu, before, "BLU", "RED")  # fix it to RED win
    assert all(st[u]["elo"] == 1616 for u in red) and all(st[u]["elo"] == 1584 for u in blu)
    assert all(st[u]["w"] == 1 and st[u]["l"] == 0 for u in red)
    assert all(st[u]["l"] == 1 and st[u]["w"] == 0 for u in blu)
    print("EE) correcting a misreported match flips Elo + W/L exactly (match-time ratings)")

    # EE2) correcting to a draw zeroes the move and records a draw for everyone.
    s.correct_match(red, blu, before, "RED", "DRAW")
    assert all(st[u]["elo"] == 1600 for u in red + blu)
    assert all(st[u]["d"] == 1 and st[u]["w"] == 0 and st[u]["l"] == 0 for u in red + blu)
    print("EE2) correcting to a draw zeroes the Elo move and records a draw")

    print("\nall smoke tests passed.")

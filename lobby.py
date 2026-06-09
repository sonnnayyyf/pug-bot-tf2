"""Cross-lobby coordination for running multiple independent PUGs at once.

Each lobby is a PugState (see pug_state.py). A lobby has its own queue, ready
check, draft and live game — they're fully independent EXCEPT for two rules that
span lobbies, enforced here:

  * Med immunity is SHARED (it's a property of the player, not the channel). The
    caller arranges this by pointing every PugState.immunity at one dict, so
    there's nothing to do in this module for it.
  * One game at a time per player. A player may sit in several lobbies' queues
    while only WAITING. The instant they're COMMITTED to a game in one lobby
    (confirmed in its ready check, or part of its draft / live match) they're
    pulled out of every OTHER lobby, and may not join any lobby until that game
    ends.

Definitions:
  COMMITTED in a lobby = confirmed in its ready check (uid in `ready`), OR part
    of a PICKING / LIVE match (uid in `queue`).
  WAITING = sitting in a queue / next_queue without being committed.

Everything here is pure logic over a {channel_id: PugState} mapping with no
Discord dependency, so it's unit-tested at the bottom (run `python lobby.py`).
"""
from pug_state import Phase, PugState


def commitment_of(states, uid):
    """The channel id of the lobby where `uid` is committed to a game, else None.
    A player can be committed in at most one lobby (this module keeps it so)."""
    for cid, st in states.items():
        if st.phase in (Phase.PICKING, Phase.LIVE) and uid in st.queue:
            return cid
        if st.phase is Phase.READY_CHECK and uid in st.ready:
            return cid
    return None


def blocking_cid(states, uid, target_cid):
    """If `uid` is already committed to a game in a DIFFERENT lobby, return that
    lobby's id (so joining `target_cid` should be refused). Otherwise None.

    Being committed in `target_cid` itself isn't 'blocking' here — the lobby's own
    add() already handles that case (you're in the current game / re-arm)."""
    home = commitment_of(states, uid)
    return home if (home is not None and home != target_cid) else None


def _committed_map(states):
    """uid -> channel_id for every committed player, across all lobbies."""
    m = {}
    for cid, st in states.items():
        if st.phase in (Phase.PICKING, Phase.LIVE):
            for u in st.queue:
                m[u] = cid
        elif st.phase is Phase.READY_CHECK:
            for u in st.ready:
                m[u] = cid
    return m


def reconcile(states):
    """Enforce one-game-at-a-time: pull every committed player out of all OTHER
    lobbies' queues. Removing an unconfirmed player from a lobby mid-ready-check
    backfills that lobby (same as an abort) and can confirm new players there, so
    we iterate to a fixed point.

    Returns (changed, notes):
      changed : {channel_id: [uids removed from that lobby]}
      notes   : [(uid, removed_from_cid, committed_in_cid)]  (for messaging)
    """
    changed, notes = {}, []
    while True:
        committed = _committed_map(states)
        did = False
        for cid, st in states.items():
            for uid, home in committed.items():
                if home == cid:
                    continue
                if uid in st.queue or uid in st.next_queue:
                    if st.phase is Phase.READY_CHECK and uid in st.queue:
                        st.abort_ready_check(uid)   # removes + backfills next queue
                    else:
                        st.remove(uid)              # QUEUING queue, or next_queue
                    changed.setdefault(cid, []).append(uid)
                    notes.append((uid, cid, home))
                    did = True
        if not did:
            return changed, notes


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    clock = {"t": 1000.0}
    now = lambda: clock["t"]

    def fresh():
        return PugState(now=now)

    def fill_live(uids):
        """A lobby filled with 12 auto-ready players -> goes straight to PICKING
        (everyone auto-confirms), i.e. all 12 are committed."""
        s = fresh()
        for u in uids:
            s.add(u, 600)
        return s

    # A) commitment_of / blocking_cid -------------------------------------- #
    A, B = 111, 222
    states = {A: fill_live(range(1, 13)), B: fresh()}
    assert states[A].phase is Phase.PICKING
    assert commitment_of(states, 1) == A           # in A's draft
    assert commitment_of(states, 99) is None        # not anywhere
    assert blocking_cid(states, 1, B) == A          # committed in A -> blocked from B
    assert blocking_cid(states, 1, A) is None       # own lobby isn't "blocking"
    print("A) commitment_of + blocking_cid")

    # B) a player waiting in two lobbies is fine until one commits ---------- #
    states = {A: fresh(), B: fresh()}
    for u in [1, 2, 3]:
        states[A].add(u, 600)                       # waiting in A
        states[B].add(u, 600)                       # waiting in B
    assert commitment_of(states, 1) is None
    changed, notes = reconcile(states)
    assert changed == {} and notes == []            # nothing committed -> no pulls
    assert 1 in states[A].queue and 1 in states[B].queue
    print("B) waiting in both lobbies is allowed")

    # C) committing in A pulls the player out of B's (QUEUING) queue -------- #
    states = {A: fresh(), B: fresh()}
    for u in [1] + list(range(20, 30)):             # B: 11 waiting incl. player 1
        states[B].add(u, 600)
    assert states[B].phase is Phase.QUEUING and 1 in states[B].queue
    for u in range(1, 13):                          # A fills -> PICKING, 1 committed
        states[A].add(u, 600)
    assert commitment_of(states, 1) == A
    changed, notes = reconcile(states)
    assert 1 not in states[B].queue                 # pulled out of B
    assert changed.get(B) == [1] and (1, B, A) in notes
    assert 20 in states[B].queue                    # everyone else in B untouched
    print("C) committing in A pulls the player out of B's queue")

    # D) pulling a waiting player from B mid-ready-check backfills B -------- #
    states = {A: fresh(), B: fresh()}
    for u in range(40, 51):                          # B: 11 auto-ready
        states[B].add(u, 600)
    states[B].add(1, 0)                              # 12th = player 1, NOT auto-ready
    assert states[B].phase is Phase.READY_CHECK and 1 not in states[B].ready
    states[B].add(60, 600); states[B].add(61, 600)   # 2 waiting in B's next queue
    assert len(states[B].next_queue) == 2
    for u in range(1, 13):                            # A commits player 1
        states[A].add(u, 600)
    changed, notes = reconcile(states)
    assert 1 not in states[B].queue                  # pulled from B
    assert 60 in states[B].queue                     # backfilled from next queue
    assert len(states[B].queue) == 12                # refilled to a full 12
    assert len(states[B].next_queue) == 1            # one backfill consumed
    assert changed.get(B) == [1]
    print("D) pulling a mid-check player backfills the lobby")

    # E) reconcile is idempotent ------------------------------------------- #
    changed2, notes2 = reconcile(states)
    assert changed2 == {} and notes2 == []
    print("E) reconcile is idempotent once settled")

    print("\nall lobby smoke tests passed.")

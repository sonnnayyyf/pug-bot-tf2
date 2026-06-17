"""
Tiny persistence layer for the PUG bot.

The bot keeps all live state in one PugState object, and the data is tiny, so
instead of normalised tables we store a single JSON snapshot in one SQLite row
(last-write-wins). This is enough to survive restarts and crashes: on boot we
load the snapshot back into PugState; while running we save after changes.

No external dependencies — sqlite3 ships with Python. The "database" is just a
file on disk (default ./pug.db) costing nothing to run.
"""

import json
import sqlite3
import time

DEFAULT_PATH = "pug.db"


class Store:
    def __init__(self, path=DEFAULT_PATH):
        # check_same_thread=False: discord.py may touch this from the loop's
        # executor; we serialise our own access and writes are trivially short.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), blob TEXT)")
        # Append-only audit log, separate from the single-row snapshot above.
        # Every row is immutable; it's how we trace what ever moved a rating
        # (a reported game, or an admin adjustment). `kind` tags the event type
        # and `blob` is its JSON payload; the bot decides the shape. The id is a
        # stable, ever-increasing match/event number that survives restarts.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, blob TEXT)")
        self._db.commit()

    def save(self, payload: dict):
        """Persist a snapshot dict. Swallows errors so a disk hiccup can never
        take the bot down — losing a save is recoverable, crashing isn't."""
        try:
            blob = json.dumps(payload)
            self._db.execute(
                "INSERT INTO snapshot (id, blob) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET blob = excluded.blob", (blob,))
            self._db.commit()
            return True
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def load(self):
        """Return the saved snapshot dict, or None if there isn't one / it's bad."""
        try:
            row = self._db.execute("SELECT blob FROM snapshot WHERE id = 1").fetchone()
            return json.loads(row[0]) if row and row[0] else None
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return None

    def close(self):
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    # ---------- append-only audit log ----------
    def log_event(self, kind: str, payload: dict):
        """Append an immutable event (e.g. a reported match, an Elo adjustment)
        and return its new id, or None on failure. Never overwrites — this is the
        audit trail, not the snapshot. Swallows errors like save() does: losing an
        audit row is recoverable, crashing the bot isn't."""
        try:
            blob = json.dumps(payload)
            cur = self._db.execute(
                "INSERT INTO events (ts, kind, blob) VALUES (?, ?, ?)",
                (time.time(), kind, blob))
            self._db.commit()
            return cur.lastrowid
        except (sqlite3.Error, TypeError, ValueError):
            return None

    def get_event(self, event_id):
        """One event by id as {id, ts, kind, data}, or None if missing/bad."""
        try:
            row = self._db.execute(
                "SELECT id, ts, kind, blob FROM events WHERE id = ?", (event_id,)).fetchone()
            if not row:
                return None
            return {"id": row[0], "ts": row[1], "kind": row[2], "data": json.loads(row[3])}
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return None

    def recent_events(self, limit=10, kind=None):
        """Most recent events first, optionally filtered by kind. Returns a list
        of {id, ts, kind, data} (empty on error)."""
        try:
            if kind is not None:
                rows = self._db.execute(
                    "SELECT id, ts, kind, blob FROM events WHERE kind = ? "
                    "ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, ts, kind, blob FROM events ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            return [{"id": r[0], "ts": r[1], "kind": r[2], "data": json.loads(r[3])}
                    for r in rows]
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return []


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    s = Store(path)

    # snapshot round-trip (last-write-wins)
    assert s.load() is None
    assert s.save({"hello": 1}) is True
    assert s.save({"hello": 2}) is True
    assert s.load() == {"hello": 2}
    print("A) snapshot saves and last-write-wins")

    # append-only event log with stable ascending ids
    m1 = s.log_event("match", {"winner": "RED", "delta": 16})
    m2 = s.log_event("elo_adjust", {"uid": 7, "before": 1000, "after": 1100})
    assert isinstance(m1, int) and m2 == m1 + 1
    assert s.get_event(m1)["data"]["winner"] == "RED"
    assert s.get_event(m2)["kind"] == "elo_adjust"
    assert s.get_event(999999) is None
    print("B) events append with ascending ids and read back by id")

    # recency + kind filter
    recent = s.recent_events(limit=10)
    assert [e["id"] for e in recent] == [m2, m1]                 # newest first
    only_matches = s.recent_events(kind="match")
    assert len(only_matches) == 1 and only_matches[0]["id"] == m1
    print("C) recent_events orders newest-first and filters by kind")

    # ids keep climbing across reopen (persisted autoincrement)
    s.close()
    s = Store(path)
    m3 = s.log_event("match", {"winner": "BLU", "delta": 8})
    assert m3 == m2 + 1
    print("D) event ids keep climbing across a reopen")

    s.close()
    os.remove(path)
    print("\nall storage smoke tests passed.")

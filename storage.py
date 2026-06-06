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

DEFAULT_PATH = "pug.db"


class Store:
    def __init__(self, path=DEFAULT_PATH):
        # check_same_thread=False: discord.py may touch this from the loop's
        # executor; we serialise our own access and writes are trivially short.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), blob TEXT)")
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

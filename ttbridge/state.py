"""Persistent sync state: item links, last-synced snapshots and key/value data."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    rule_id     TEXT NOT NULL,
    things_id   TEXT NOT NULL UNIQUE,
    todoist_id  TEXT NOT NULL UNIQUE,
    snapshot    TEXT NOT NULL,
    title       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS links_rule ON links(rule_id);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- links -----------------------------------------------------------
    def links(self, rule_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if rule_id is None:
                rows = self.db.execute("SELECT * FROM links ORDER BY updated_at DESC").fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM links WHERE rule_id=? ORDER BY updated_at DESC", (rule_id,)
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = json.loads(d["snapshot"])
            out.append(d)
        return out

    def linked_things_ids(self) -> set:
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT things_id FROM links")}

    def linked_todoist_ids(self) -> set:
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT todoist_id FROM links")}

    def add_link(self, rule_id: str, things_id: str, todoist_id: str, snapshot: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?)",
                (rule_id, things_id, todoist_id, json.dumps(snapshot, ensure_ascii=False),
                 snapshot.get("title"), now, now),
            )
            self.db.commit()

    def update_snapshot(self, things_id: str, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE links SET snapshot=?, title=?, updated_at=? WHERE things_id=?",
                (json.dumps(snapshot, ensure_ascii=False), snapshot.get("title"), time.time(), things_id),
            )
            self.db.commit()

    def remove_link(self, things_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM links WHERE things_id=?", (things_id,))
            self.db.commit()

    def remove_rule(self, rule_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM links WHERE rule_id=?", (rule_id,))
            self.db.commit()

    def counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self.db.execute("SELECT rule_id, COUNT(*) FROM links GROUP BY rule_id").fetchall()
        return {r[0]: r[1] for r in rows}

    # ---- key/value -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False))
            )
            self.db.commit()

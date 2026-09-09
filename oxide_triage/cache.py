"""SQLite cache. Every remote record is stored here before anything downstream sees it.

Design:
  * One generic ``records`` table keyed by (source, key). Payload is JSON. Keeping the
    store schema-agnostic means a new source needs no migration.
  * ``retrieved_at`` on every row gives the audit view its timestamps and drives TTL.
  * ``meta`` holds flags such as ``fixture_loaded`` so synthetic development data can
    never masquerade as real data downstream.
  * ``fingerprint`` hashes the rows a run actually touched, so two results can be
    compared for "same cache state".
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FIXTURE_FLAG = "fixture_loaded"


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Cache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init()
        self.touched: set[tuple[str, str]] = set()

    def _init(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                source TEXT NOT NULL,
                key TEXT NOT NULL,
                payload TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY (source, key)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                key TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self._conn.commit()

    # ---- records ---------------------------------------------------------------------

    def get(self, source: str, key: str) -> tuple[Any, str] | None:
        row = self._conn.execute(
            "SELECT payload, retrieved_at FROM records WHERE source=? AND key=?", (source, key)
        ).fetchone()
        if row is None:
            return None
        self.touched.add((source, key))
        return json.loads(row["payload"]), row["retrieved_at"]

    def put(self, source: str, key: str, payload: Any, retrieved_at: str | None = None) -> str:
        ts = retrieved_at or utcnow_iso()
        self._conn.execute(
            "INSERT OR REPLACE INTO records (source, key, payload, retrieved_at) VALUES (?,?,?,?)",
            (source, key, json.dumps(payload, sort_keys=True, default=str), ts),
        )
        self._conn.commit()
        self.touched.add((source, key))
        return ts

    def keys(self, source: str, prefix: str = "") -> list[str]:
        rows = self._conn.execute(
            "SELECT key FROM records WHERE source=? AND key LIKE ? ORDER BY key",
            (source, f"{prefix}%"),
        ).fetchall()
        return [r["key"] for r in rows]

    def is_fresh(self, retrieved_at: str, ttl_days: int) -> bool:
        if ttl_days <= 0:
            return True
        try:
            ts = datetime.fromisoformat(retrieved_at)
        except ValueError:
            return True  # fixture rows carry a non-timestamp marker; never expire them
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return datetime.now(UTC) - ts < timedelta(days=ttl_days)

    def count(self, source: str | None = None) -> int:
        if source is None:
            return self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return self._conn.execute(
            "SELECT COUNT(*) FROM records WHERE source=?", (source,)
        ).fetchone()[0]

    def sources_summary(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) AS n, MIN(retrieved_at) AS oldest, MAX(retrieved_at) AS newest "
            "FROM records GROUP BY source ORDER BY source"
        ).fetchall()
        return {r["source"]: {"n": r["n"], "oldest": r["oldest"], "newest": r["newest"]} for r in rows}

    # ---- log & meta ------------------------------------------------------------------

    def log(self, source: str, key: str, outcome: str, detail: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO fetch_log (ts, source, key, outcome, detail) VALUES (?,?,?,?,?)",
            (utcnow_iso(), source, key, outcome, detail),
        )
        self._conn.commit()

    def set_meta(self, k: str, v: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, v))
        self._conn.commit()

    def get_meta(self, k: str) -> str | None:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return None if row is None else row["v"]

    @property
    def has_fixture_data(self) -> bool:
        return self.get_meta(FIXTURE_FLAG) == "1"

    # ---- reproducibility -------------------------------------------------------------

    def fingerprint(self, touched: Iterable[tuple[str, str]] | None = None) -> str:
        """Hash of the payload+timestamp of every row this run read or wrote."""
        pairs = sorted(set(touched) if touched is not None else self.touched)
        h = hashlib.sha256()
        for source, key in pairs:
            row = self._conn.execute(
                "SELECT payload, retrieved_at FROM records WHERE source=? AND key=?",
                (source, key),
            ).fetchone()
            if row is None:
                continue
            h.update(f"{source}\x00{key}\x00{row['retrieved_at']}\x00{row['payload']}\n".encode())
        return h.hexdigest()[:16]

    def close(self) -> None:
        self._conn.close()

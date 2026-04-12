"""SQLite-backed metadata cache for the reference resolver.

Replaces the previous scheme of one JSON file per cache entry. Benefits:

- **Fewer filesystem calls.** A single SQLite file serves all lookups,
  eliminating hundreds of `stat` / `open` / `json.loads` cycles per run.
- **Atomic writes.** No more half-written JSONs on crash.
- **Thread-safe.** A single connection shared across threads, guarded by
  a lock — the workload is low enough that the lock is never a bottleneck.
- **Negative caching.** ``None`` is a legitimate value, so resolver
  failures ("we searched arxiv and found nothing") are cached and not
  retried on every run.

PDFs are still stored on disk in ``cache/pdfs/`` — SQLite is a poor fit
for megabytes of binary data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MetadataCache:
    """Minimal key-value store over SQLite, keyed by (source, key)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                source TEXT NOT NULL,
                key TEXT NOT NULL,
                data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, key)
            )
            """
        )

    def get(self, source: str, key: str) -> tuple[bool, Any]:
        """Return (hit, value). `hit` is True iff the key is in the cache.
        `value` can be None when a negative result ("we looked and there's
        nothing there") was cached."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM metadata WHERE source = ? AND key = ?",
                (source, key),
            ).fetchone()
        if row is None:
            return (False, None)
        raw = row[0]
        if raw is None:
            return (True, None)
        try:
            return (True, json.loads(raw))
        except Exception:
            logger.warning("corrupt cache entry %s/%s, treating as miss", source, key)
            return (False, None)

    def set(self, source: str, key: str, value: Any) -> None:
        """Store a value. `None` is legal and records a negative hit."""
        payload = None if value is None else json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata (source, key, data) VALUES (?, ?, ?)",
                (source, key, payload),
            )

    def purge_negatives(self, *sources: str) -> int:
        """Delete all entries with ``data IS NULL`` for the given sources.

        Negative cache entries from external searches (arxiv, OpenReview)
        are unreliable: a transient network hiccup or a one-time service
        outage can persist a "not found" verdict for a paper that actually
        is reachable. Call this once at startup to self-heal such entries.

        Returns the number of rows deleted.
        """
        if not sources:
            return 0
        placeholders = ",".join("?" for _ in sources)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM metadata WHERE data IS NULL AND source IN ({placeholders})",
                sources,
            )
            return cur.rowcount

    def purge_all(self, source: str) -> int:
        """Delete ALL entries for a given source. Returns rows deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM metadata WHERE source = ?", (source,),
            )
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

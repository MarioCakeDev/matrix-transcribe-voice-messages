"""Durable record of the Matrix events this bot has already handled.

The sync loop can hand the same room event to ``handle_message`` more than once:
on a restart the persisted mautrix sync token may be missing (an initial sync
replays the room's recent timeline), and a crypto reset can lose it as well.
Without a record of what was already transcribed, every such replay produced a
duplicate transcription — the owner's "on restart it re-transcribes" report.

This store is deliberately *separate* from ``crypto.db``: the crypto account is
exactly the thing that gets recreated when E2EE breaks, and the dedupe record
has to survive that. It lives in ``$STORE_PATH/processed.db`` (the same named
volume), so it also survives a container recreate.

Semantics — check before work, mark after the reply:

* :meth:`ProcessedStore.claim` inserts a ``processing`` row and returns ``True``
  when the event has never been seen. A row left behind by a crash is retried
  once it is older than ``stale_seconds``, so a hard kill mid-transcription does
  not lose the event forever.
* :meth:`ProcessedStore.complete` records the outcome (``replied`` or
  ``failed``) once the reply is on the wire.
* :meth:`ProcessedStore.prune` bounds the file by age and by row count.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DB_FILENAME = "processed.db"
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_ROWS = 20_000
# A ``processing`` row older than this is assumed to belong to a run that died
# before it could reply, so the event is retried instead of being lost forever.
DEFAULT_STALE_SECONDS = 900.0

STATUS_PROCESSING = "processing"
STATUS_REPLIED = "replied"
STATUS_FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_event (
    event_id      TEXT PRIMARY KEY,
    room_id       TEXT NOT NULL,
    status        TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    completed_at  REAL
);
CREATE INDEX IF NOT EXISTS processed_event_completed_at
    ON processed_event (completed_at);
CREATE INDEX IF NOT EXISTS processed_event_status
    ON processed_event (status);
"""


def database_path(store_path: str | Path) -> Path:
    """Return the dedupe database path for a given store directory."""
    return Path(store_path) / DB_FILENAME


class ProcessedStore:
    """A small SQLite table of event ids this bot has handled."""

    def __init__(
        self,
        path: str | Path,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if stale_seconds < 0:
            raise ValueError("stale_seconds must be >= 0")
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self._clock = clock
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "ProcessedStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        try:
            # WAL keeps a read (the healthcheck) off the writer's back.
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:  # pragma: no cover - exotic filesystems
            logger.warning("Could not enable WAL on %s", self.path)
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "ProcessedStore":
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ProcessedStore is not open; call open() first")
        return self._conn

    # -- queries -----------------------------------------------------------
    def seen(self, event_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_event WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None

    def status_of(self, event_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM processed_event WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else str(row["status"])

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM processed_event").fetchone()[0])

    # -- mutation ----------------------------------------------------------
    def claim(self, event_id: str, room_id: str, now: float | None = None) -> bool:
        """Take ownership of *event_id*; return ``False`` if it was handled.

        ``True`` means the caller should do the work now. The row is written
        *before* the work starts so a concurrent delivery of the same event
        cannot start a second transcription.
        """
        if not event_id:
            # Nothing to dedupe on; better to transcribe than to drop silently.
            return True
        ts = self._clock() if now is None else now
        row = self.conn.execute(
            "SELECT status, first_seen_at FROM processed_event WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO processed_event (event_id, room_id, status, first_seen_at)"
                " VALUES (?, ?, ?, ?)",
                (event_id, room_id, STATUS_PROCESSING, ts),
            )
            return True
        if row["status"] == STATUS_PROCESSING and ts - float(row["first_seen_at"]) > self.stale_seconds:
            logger.warning(
                "Retrying voice message %s in %s: a previous run started it %.0fs ago and never finished",
                event_id,
                room_id,
                ts - float(row["first_seen_at"]),
            )
            self.conn.execute(
                "UPDATE processed_event SET status = ?, first_seen_at = ?, completed_at = NULL"
                " WHERE event_id = ?",
                (STATUS_PROCESSING, ts, event_id),
            )
            return True
        return False

    def complete(
        self,
        event_id: str,
        *,
        status: str = STATUS_REPLIED,
        now: float | None = None,
    ) -> None:
        if not event_id:
            return
        ts = self._clock() if now is None else now
        self.conn.execute(
            "UPDATE processed_event SET status = ?, completed_at = ? WHERE event_id = ?",
            (status, ts, event_id),
        )

    # -- housekeeping ------------------------------------------------------
    def prune(
        self,
        *,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
        max_rows: int = DEFAULT_MAX_ROWS,
        now: float | None = None,
    ) -> int:
        """Drop old rows and cap the table; return the number deleted.

        Only *completed* rows are pruned by age — an unfinished ``processing``
        row is the only evidence that an event was ever seen, so it stays until
        the row cap evicts it.
        """
        if max_age_days < 0:
            raise ValueError("max_age_days must be >= 0")
        if max_rows < 1:
            raise ValueError("max_rows must be >= 1")
        ts = self._clock() if now is None else now
        deleted = self.conn.execute(
            "DELETE FROM processed_event"
            " WHERE completed_at IS NOT NULL AND completed_at < ?",
            (ts - max_age_days * 86_400.0,),
        ).rowcount
        deleted += self.conn.execute(
            "DELETE FROM processed_event WHERE event_id IN ("
            "  SELECT event_id FROM processed_event"
            "  ORDER BY COALESCE(completed_at, first_seen_at) ASC"
            "  LIMIT MAX((SELECT COUNT(*) FROM processed_event) - ?, 0)"
            ")",
            (max_rows,),
        ).rowcount
        return max(0, deleted)


__all__ = [
    "ProcessedStore",
    "database_path",
    "STATUS_FAILED",
    "STATUS_PROCESSING",
    "STATUS_REPLIED",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_STALE_SECONDS",
]

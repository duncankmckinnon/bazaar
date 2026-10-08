"""SQLite store for submissions and board events.

Each operation opens its own connection, so worker threads can report progress safely.
"""

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import logfire

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    handle TEXT,
    instructions TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    day INTEGER,
    error TEXT,
    run_dir TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    latest_value TEXT,
    trace_context TEXT
);
CREATE TABLE IF NOT EXISTS events (
    t TEXT NOT NULL,
    text TEXT NOT NULL,
    submission_id TEXT
);
"""

Now = Callable[[], datetime]


class NameTaken(Exception):
    pass


class CapReached(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class TracedConnection:
    """A connection whose queries emit sqlite spans.

    logfire.instrument_sqlite3 traces cursor().execute only, not Connection.execute, so every
    query goes through a cursor. Transaction control uses the raw connection: no span each.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.raw = conn
        self.traced = logfire.instrument_sqlite3(conn)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.traced.cursor().execute(sql, params)

    def executescript(self, script: str) -> sqlite3.Cursor:
        return self.raw.executescript(script)

    def close(self) -> None:
        self.raw.close()


class Store:
    def __init__(self, path: Path, now: Now = lambda: datetime.now(UTC)) -> None:
        self.path = path
        self.now = now
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database created by an older release up to date. Safe to run every start.

        CREATE TABLE IF NOT EXISTS never adds columns to an existing table, so each later
        column is added here when missing. Existing rows are kept (new columns start NULL).
        """
        with self._tx() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
            for column in ("latest_value", "trace_context"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE submissions ADD COLUMN {column} TEXT")

    def _connect(self) -> "TracedConnection":
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return TracedConnection(conn)

    @contextmanager
    def _tx(self) -> Iterator["TracedConnection"]:
        with closing(self._connect()) as conn:
            conn.raw.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.raw.execute("ROLLBACK")
                raise
            conn.raw.execute("COMMIT")

    def _stamp(self) -> str:
        return self.now().isoformat()

    def create(
        self,
        *,
        name: str,
        handle: str | None,
        instructions: str,
        ip_hash: str,
        max_queue: int,
        max_per_day: int,
        max_per_ip_hour: int,
        trace_context: str | None = None,
    ) -> str:
        """Insert a queued submission, checking the name and every cap in one transaction."""
        now = self.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM submissions WHERE name = ?", (name,)).fetchone():
                raise NameTaken
            (in_flight,) = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE status IN ('queued', 'running')"
            ).fetchone()
            if in_flight >= max_queue:
                raise CapReached("The queue is full right now. Please try again in a few minutes.")
            (today,) = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE created_at >= ?", (midnight.isoformat(),)
            ).fetchone()
            if today >= max_per_day:
                raise CapReached("Today's submission limit has been reached. Thanks for playing!")
            (recent,) = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE ip_hash = ? AND created_at >= ?",
                (ip_hash, (now - timedelta(hours=1)).isoformat()),
            ).fetchone()
            if recent >= max_per_ip_hour:
                raise CapReached(
                    "Too many submissions from your network in the last hour. Please wait a bit."
                )
            submission_id = uuid4().hex
            conn.execute(
                "INSERT INTO submissions (id, name, handle, instructions, ip_hash, status, "
                "created_at, trace_context) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    submission_id,
                    name,
                    handle,
                    instructions,
                    ip_hash,
                    now.isoformat(),
                    trace_context,
                ),
            )
            self._event(conn, f"{name} joined the queue", submission_id)
        return submission_id

    def _event(self, conn: TracedConnection, text: str, submission_id: str | None) -> None:
        conn.execute(
            "INSERT INTO events (t, text, submission_id) VALUES (?, ?, ?)",
            (self._stamp(), text, submission_id),
        )

    def add_event(self, text: str, submission_id: str | None = None) -> None:
        with self._tx() as conn:
            self._event(conn, text, submission_id)

    def get(self, submission_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()
        return dict(row) if row else None

    def position(self, submission_id: str) -> int | None:
        """1-based FIFO position among queued submissions, or None when not queued."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM submissions AS q, submissions AS me "
                "WHERE me.id = ? AND me.status = 'queued' AND q.status = 'queued' "
                "AND q.rowid <= me.rowid",
                (submission_id,),
            ).fetchone()
        return row[0] or None

    def mark_running(self, submission_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE submissions SET status = 'running', started_at = ? WHERE id = ?",
                (self._stamp(), submission_id),
            )

    def set_day(self, submission_id: str, day: int, value: Decimal | None = None) -> bool:
        """Record progress and, when given, the marked portfolio value; True if the day changed."""
        with self._tx() as conn:
            changed = conn.execute(
                "UPDATE submissions SET day = ?, latest_value = COALESCE(?, latest_value) "
                "WHERE id = ? AND day IS NOT ?",
                (day, None if value is None else str(value), submission_id, day),
            ).rowcount
        return bool(changed)

    def finish(self, submission_id: str, *, run_dir: str | None, error: str | None) -> None:
        status = "failed" if error else "scored"
        with self._tx() as conn:
            conn.execute(
                "UPDATE submissions SET status = ?, run_dir = ?, error = ?, finished_at = ? "
                "WHERE id = ?",
                (status, run_dir, error, self._stamp(), submission_id),
            )

    def hide(self, submission_id: str) -> bool:
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE submissions SET hidden = 1 WHERE id = ?", (submission_id,)
                ).rowcount
            )

    def ids_with_status(self, status: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id FROM submissions WHERE status = ? ORDER BY rowid", (status,)
            ).fetchall()
        return [row["id"] for row in rows]

    def requeue(self, submission_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE submissions SET status = 'queued', day = NULL, started_at = NULL, "
                "latest_value = NULL "
                "WHERE id = ? AND status = 'running'",
                (submission_id,),
            )

    def in_flight(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE status IN ('queued', 'running') AND hidden = 0 "
                "ORDER BY rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def by_run_dir(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM submissions WHERE run_dir IS NOT NULL").fetchall()
        return {row["run_dir"]: dict(row) for row in rows}

    def recent_events(self, limit: int = 20) -> list[dict[str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT e.t, e.text FROM events AS e "
                "LEFT JOIN submissions AS s ON s.id = e.submission_id "
                "WHERE COALESCE(s.hidden, 0) = 0 ORDER BY e.rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"t": row["t"], "text": row["text"]} for row in rows]

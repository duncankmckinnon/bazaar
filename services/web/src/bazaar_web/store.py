"""SQLite store for submissions and board events.

Each operation opens its own connection, so worker threads can report progress safely.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import logfire

from bazaar_web.house import house_name

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
    trace_context TEXT,
    rerun_of TEXT,
    error_class TEXT,
    lane TEXT DEFAULT 'human',
    -- Personal data, for contacting winners: kept in this database only, never shown publicly,
    -- sent to the runner, traced or logged. Null on house runs and on rows from before them.
    email TEXT,
    company TEXT,
    role TEXT
);
CREATE TABLE IF NOT EXISTS events (
    t TEXT NOT NULL,
    text TEXT NOT NULL,
    submission_id TEXT
);
-- The house lane's place in the house strategy list, and whether an admin paused it.
CREATE TABLE IF NOT EXISTS house_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cursor INTEGER NOT NULL,
    cycle INTEGER NOT NULL,
    paused INTEGER NOT NULL,
    total_started INTEGER NOT NULL
);
INSERT OR IGNORE INTO house_state (id, cursor, cycle, paused, total_started) VALUES (1, 0, 1, 0, 0);
-- Marked portfolio value after each session of a running submission, for the live chart.
CREATE TABLE IF NOT EXISTS submission_progress (
    submission_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (submission_id, day)
);
"""

Now = Callable[[], datetime]

log = logging.getLogger("bazaar_web.store")


class LockErrors:
    """Counts SQLite "database is locked" / "busy" errors in this process. Measures only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @contextmanager
    def counted(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                with self._lock:
                    self._count += 1
                log.warning("sqlite lock error: %s", exc)
            raise


LOCK_ERRORS = LockErrors()


class NameTaken(Exception):
    pass


class NotFound(Exception):
    pass


class StillInFlight(Exception):
    pass


class Hidden(Exception):
    """Already rerun (its replacement is the live row) or hidden by moderation."""


# The fixed contact the operator scripts (smoke test, house strategies) send. example.com is
# reserved, never a real inbox; the contacts CSV leaves these rows out.
OPERATOR_CONTACT_EMAIL = "bazaar-ops@example.com"


@dataclass(frozen=True)
class Contact:
    """A submitter's contact details. Personal data: see the email column."""

    email: str | None = None
    company: str | None = None
    role: str | None = None

    def __repr__(self) -> str:
        return "Contact(...)"  # never print personal data, even in a traceback


class HouseRun(Exception):
    """A house-lane row: the lane refills itself, so there is nothing to rerun."""


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
        with LOCK_ERRORS.counted():
            return self.traced.cursor().execute(sql, params)

    def control(self, sql: str) -> None:
        """BEGIN, COMMIT or ROLLBACK, untraced."""
        with LOCK_ERRORS.counted():
            self.raw.execute(sql)

    def executescript(self, script: str) -> sqlite3.Cursor:
        with LOCK_ERRORS.counted():
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
            for column in (
                "latest_value",
                "trace_context",
                "rerun_of",
                "error_class",
                "email",
                "company",
                "role",
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE submissions ADD COLUMN {column} TEXT")
            if "lane" not in columns:  # existing rows are all human submissions
                conn.execute("ALTER TABLE submissions ADD COLUMN lane TEXT DEFAULT 'human'")

    def _connect(self) -> "TracedConnection":
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return TracedConnection(conn)

    @contextmanager
    def _tx(self) -> Iterator["TracedConnection"]:
        with closing(self._connect()) as conn:
            conn.control("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.control("ROLLBACK")
                raise
            conn.control("COMMIT")

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
        contact: Contact | None = None,
    ) -> str:
        """Insert a queued submission, checking the name and every cap in one transaction."""
        contact = contact or Contact()
        now = self.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM submissions WHERE name = ?", (name,)).fetchone():
                raise NameTaken
            # Admin reruns occupy the queue, so they count here, but not in the daily or per-IP caps.
            # House runs have their own slots and count toward no cap.
            (in_flight,) = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE status IN ('queued', 'running') "
                "AND lane = 'human'"
            ).fetchone()
            if in_flight >= max_queue:
                raise CapReached("The queue is full right now. Please try again in a few minutes.")
            (today,) = conn.execute(
                "SELECT COUNT(*) FROM submissions "
                "WHERE created_at >= ? AND rerun_of IS NULL AND lane = 'human'",
                (midnight.isoformat(),),
            ).fetchone()
            if today >= max_per_day:
                raise CapReached("Today's submission limit has been reached. Thanks for playing!")
            (recent,) = conn.execute(
                "SELECT COUNT(*) FROM submissions "
                "WHERE ip_hash = ? AND created_at >= ? AND rerun_of IS NULL AND lane = 'human'",
                (ip_hash, (now - timedelta(hours=1)).isoformat()),
            ).fetchone()
            if recent >= max_per_ip_hour:
                raise CapReached(
                    "Too many submissions from your network in the last hour. Please wait a bit."
                )
            submission_id = uuid4().hex
            conn.execute(
                "INSERT INTO submissions (id, name, handle, instructions, ip_hash, status, "
                "created_at, trace_context, email, company, role, lane) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, 'human')",
                (
                    submission_id,
                    name,
                    handle,
                    instructions,
                    ip_hash,
                    now.isoformat(),
                    trace_context,
                    contact.email,
                    contact.company,
                    contact.role,
                ),
            )
            self._event(conn, f"{name} joined the queue", submission_id)
        return submission_id

    def create_house(
        self, catalog: Sequence[tuple[str, str]], max_per_hour: int | None
    ) -> tuple[str, str] | None:
        """Insert the next house strategy as a queued house-lane row; (id, name), or None when
        the lane is paused or has started max_per_hour runs in the last hour.

        The cursor walks the catalog; past its end the cycle goes up, so names never repeat. A
        name already taken (say, by an attendee) is skipped.
        """
        now = self.now()
        with self._tx() as conn:
            state = conn.execute("SELECT * FROM house_state WHERE id = 1").fetchone()
            if state["paused"]:
                return None
            if max_per_hour is not None:
                (recent,) = conn.execute(
                    "SELECT COUNT(*) FROM submissions WHERE lane = 'house' AND created_at >= ?",
                    ((now - timedelta(hours=1)).isoformat(),),
                ).fetchone()
                if recent >= max_per_hour:
                    return None
            cursor, cycle = state["cursor"], state["cycle"]
            while True:
                slug, instructions = catalog[cursor]
                name = house_name(slug, cycle)
                cursor += 1
                if cursor == len(catalog):
                    cursor, cycle = 0, cycle + 1
                taken = conn.execute("SELECT 1 FROM submissions WHERE name = ?", (name,))
                if not taken.fetchone():
                    break
            submission_id = uuid4().hex
            conn.execute(
                "INSERT INTO submissions (id, name, handle, instructions, ip_hash, status, "
                "created_at, lane) VALUES (?, ?, 'house', ?, 'house', 'queued', ?, 'house')",
                (submission_id, name, instructions, now.isoformat()),
            )
            conn.execute(
                "UPDATE house_state SET cursor = ?, cycle = ?, total_started = total_started + 1 "
                "WHERE id = 1",
                (cursor, cycle),
            )
        return submission_id, name

    def house_state(self) -> dict[str, Any]:
        """The lane's cursor, cycle, pause flag and counts (running, started in the last hour)."""
        hour_ago = (self.now() - timedelta(hours=1)).isoformat()
        with closing(self._connect()) as conn:
            state = dict(conn.execute("SELECT * FROM house_state WHERE id = 1").fetchone())
            (running,) = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE lane = 'house' AND status = 'running'"
            ).fetchone()
            (recent,) = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE lane = 'house' AND created_at >= ?",
                (hour_ago,),
            ).fetchone()
        del state["id"]
        return {**state, "paused": bool(state["paused"]), "running": running, "last_hour": recent}

    def set_house_paused(self, paused: bool) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE house_state SET paused = ? WHERE id = 1", (int(paused),))

    def lane_counts(self) -> dict[str, dict[str, int]]:
        """{lane: {status: n}} for in-flight rows."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT lane, status, COUNT(*) AS n FROM submissions "
                "WHERE status IN ('queued', 'running') GROUP BY lane, status"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {"human": {}, "house": {}}
        for row in rows:
            counts.setdefault(row["lane"] or "human", {})[row["status"]] = row["n"]
        return counts

    def rerun(self, old_id: str, *, trace_context: str | None = None) -> str:
        """Queue a finished submission again under the same name; return the new id.

        In one transaction: the old row is renamed "<name>~<old id prefix>" and hidden, which
        frees the name and keeps its run dir off the board, and a new queued row copies its
        name, handle, instructions and ip hash. The new id gives the run new experiment and run
        ids. An admin action: no caps apply, and reruns never count toward the daily or
        per-IP caps of later submissions.
        """
        with self._tx() as conn:
            old = conn.execute("SELECT * FROM submissions WHERE id = ?", (old_id,)).fetchone()
            if old is None:
                raise NotFound
            # A rerun hides the old row, so this refuses a repeat rerun of the same id (a second
            # paid run under the "~" name) and never un-hides a moderated name.
            if old["hidden"]:
                raise Hidden
            if old["lane"] == "house":
                raise HouseRun
            if old["status"] in ("queued", "running"):
                raise StillInFlight
            conn.execute(
                "UPDATE submissions SET name = ?, hidden = 1 WHERE id = ?",
                (f"{old['name']}~{old_id[:8]}", old_id),
            )
            new_id = uuid4().hex
            conn.execute(
                "INSERT INTO submissions (id, name, handle, instructions, ip_hash, status, "
                "created_at, trace_context, rerun_of, email, company, role) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    old["name"],
                    old["handle"],
                    old["instructions"],
                    old["ip_hash"],
                    self._stamp(),
                    trace_context,
                    old_id,
                    old["email"],  # the same person's run: their contact details carry over
                    old["company"],
                    old["role"],
                ),
            )
            self._event(conn, f"{old['name']} re-queued", new_id)
        return new_id

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
                "AND q.lane = 'human' AND me.lane = 'human' AND q.rowid <= me.rowid",
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
            if value is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO submission_progress (submission_id, day, value) "
                    "VALUES (?, ?, ?)",
                    (submission_id, day, str(value)),
                )
        return bool(changed)

    def progress(self, submission_ids: list[str]) -> dict[str, list[tuple[int, str]]]:
        """(day, value) pairs in day order for each submission that reported values."""
        if not submission_ids:
            return {}
        marks = ", ".join("?" * len(submission_ids))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT submission_id, day, value FROM submission_progress "
                f"WHERE submission_id IN ({marks}) ORDER BY submission_id, day",
                submission_ids,
            ).fetchall()
        found: dict[str, list[tuple[int, str]]] = {}
        for row in rows:
            found.setdefault(row["submission_id"], []).append((row["day"], row["value"]))
        return found

    def finish(
        self,
        submission_id: str,
        *,
        run_dir: str | None,
        error: str | None,
        error_class: str | None = None,
    ) -> None:
        """error is the public text; error_class only the exception's class name, for stats."""
        status = "failed" if error else "scored"
        with self._tx() as conn:
            conn.execute(
                "UPDATE submissions SET status = ?, run_dir = ?, error = ?, error_class = ?, "
                "finished_at = ? WHERE id = ?",
                (status, run_dir, error, error_class, self._stamp(), submission_id),
            )

    def hide(self, submission_id: str) -> bool:
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE submissions SET hidden = 1 WHERE id = ?", (submission_id,)
                ).rowcount
            )

    def ids_with_status(self, status: str, lane: str = "human") -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id FROM submissions WHERE status = ? AND lane = ? ORDER BY rowid",
                (status, lane),
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
            # The run starts over, so its chart line does too.
            conn.execute(
                "DELETE FROM submission_progress WHERE submission_id = ?", (submission_id,)
            )

    def timings(self) -> list[dict[str, Any]]:
        """Every submission's status, timestamps and failure class, hidden ones included."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, status, created_at, started_at, finished_at, error_class, "
                "rerun_of, lane FROM submissions ORDER BY rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def in_flight(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM submissions WHERE status IN ('queued', 'running') AND hidden = 0 "
                "ORDER BY rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def contacts(self) -> list[dict[str, Any]]:
        """Visible submissions that left contact details, oldest first (for the admin CSV).

        Operator rows are left out. House runs never carry contact details, so the NOT NULL test
        already leaves them out too.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, handle, email, company, role, created_at FROM submissions "
                "WHERE email IS NOT NULL AND lower(email) != ? AND hidden = 0 ORDER BY rowid",
                (OPERATOR_CONTACT_EMAIL,),
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

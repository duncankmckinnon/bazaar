"""SQLite access for the market's experiment and account tables.

Every write runs inside `write_transaction`, which takes the write lock with BEGIN IMMEDIATE before
the first read, so two writers can never both pass a balance check.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import logfire
from bazaar_protocol import ErrorCode

SCHEMA = """
CREATE TABLE IF NOT EXISTS acct_experiments (
    experiment_id TEXT PRIMARY KEY,
    data_version TEXT NOT NULL,
    execution_rule_version TEXT NOT NULL,
    cutoff_at TEXT NOT NULL,
    cutoff_seq INTEGER NOT NULL CHECK (cutoff_seq >= 1)
);
CREATE TABLE IF NOT EXISTS acct_cutoff_history (
    experiment_id TEXT NOT NULL REFERENCES acct_experiments(experiment_id),
    cutoff_seq INTEGER NOT NULL CHECK (cutoff_seq >= 1),
    cutoff_at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, cutoff_seq),
    UNIQUE (experiment_id, cutoff_at)
);
CREATE TRIGGER IF NOT EXISTS acct_cutoff_history_immutable_update
BEFORE UPDATE ON acct_cutoff_history
BEGIN SELECT RAISE(ABORT, 'cutoff history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS acct_cutoff_history_immutable_delete
BEFORE DELETE ON acct_cutoff_history
BEGIN SELECT RAISE(ABORT, 'cutoff history is append-only'); END;
"""


class MarketError(Exception):
    """A refused request. Nothing was written."""

    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def format_time(value: datetime) -> str:
    """The one stored timestamp format, so stored values compare correctly as strings."""
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database_path, timeout=30, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


class _Statements:
    """Routes statements through cursors of a connection from logfire.instrument_sqlite3, which
    traces cursor().execute but not connection.execute, and counts them."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        # No kwargs: logfire's per-connection path drops them, and OTel's defaults keep parameter
        # capture off. tests/test_telemetry.py fails if bound values ever reach a span.
        self._connection = logfire.instrument_sqlite3(connection)
        self.count = 0

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        self.count += 1
        return self._connection.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: object) -> sqlite3.Cursor:
        self.count += 1
        return self._connection.cursor().executemany(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


@contextmanager
def _span(
    kind: str, operation: str | None, connection: sqlite3.Connection
) -> Iterator[sqlite3.Connection]:
    """With an operation: one span for the transaction, and a child span per statement."""
    if operation is None:
        yield connection
        return
    statements = _Statements(connection)
    with logfire.span("ledger db {kind}", kind=kind, operation=operation) as span:
        try:
            yield statements  # type: ignore[misc]
        finally:
            span.set_attribute("statement_count", statements.count)


def initialize(database_path: Path, *schemas: str) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database_path)
    try:
        for schema in (SCHEMA, *schemas):
            connection.executescript(schema)
    finally:
        connection.close()


@contextmanager
def write_transaction(
    database_path: Path, *, operation: str | None = None
) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            with _span("write", operation, connection) as traced:
                yield traced
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


@contextmanager
def read_connection(
    database_path: Path, *, operation: str | None = None
) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        with _span("read", operation, connection) as traced:
            yield traced
    finally:
        connection.close()

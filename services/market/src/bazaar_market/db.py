"""SQLite access for the market's experiment and account tables.

Every write runs inside `write_transaction`, which takes the write lock with BEGIN IMMEDIATE before
the first read, so two writers can never both pass a balance check.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def initialize(database_path: Path, *schemas: str) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database_path)
    try:
        for schema in (SCHEMA, *schemas):
            connection.executescript(schema)
    finally:
        connection.close()


@contextmanager
def write_transaction(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


@contextmanager
def read_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        yield connection
    finally:
        connection.close()

"""Archived news: storage and reads cut off at a trusted simulated time.

An article is selected by its publication time and is visible only once the revision on file
was available. Alpaca keeps only the latest revision, so an article revised after the cutoff is
left out rather than served in a state the archive does not have.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .archive import FutureDataError, MissingCoverage, stored_time

NEWS_VERSION = "alpaca-news-v1"
NEWS_SOURCE = "alpaca-news"
TEXT_LIMIT = 100_000
HEADLINE_LIMIT = 4096

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_news (
    data_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    published_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    headline TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol, record_id)
);
CREATE TABLE IF NOT EXISTS data_news_coverage (
    data_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol)
);
"""


class NewsConflict(Exception):
    """An import would change an article or window already stored under the same version."""


@dataclass(frozen=True)
class NewsRecord:
    symbol: str
    record_id: str
    revision: str
    published_at: datetime
    available_at: datetime
    headline: str
    text: str


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def import_news(
    connection: sqlite3.Connection,
    records: Iterable[NewsRecord],
    coverage: dict[str, tuple[datetime, datetime]],
    *,
    data_version: str = NEWS_VERSION,
) -> int:
    """Store articles and each symbol's fetched window. Re-importing the same data is a no-op.

    A changed article or window raises `NewsConflict`, and nothing is written. Returns the
    number of articles newly stored.
    """
    ensure_schema(connection)
    added = 0
    with connection:
        for symbol, (start, end) in coverage.items():
            window = (data_version, symbol, stored_time(start), stored_time(end))
            stored = connection.execute(
                "SELECT * FROM data_news_coverage WHERE data_version = ? AND symbol = ?",
                (data_version, symbol),
            ).fetchone()
            if stored is None:
                connection.execute("INSERT INTO data_news_coverage VALUES (?, ?, ?, ?)", window)
            elif tuple(stored) != window:
                raise NewsConflict(f"{symbol} already has a different window in {data_version}")
        for r in records:
            if r.symbol not in coverage:
                raise NewsConflict(f"{r.symbol} has articles but no fetched window")
            row = (
                data_version,
                r.symbol,
                r.record_id,
                r.revision,
                stored_time(r.published_at),
                stored_time(r.available_at),
                r.headline,
                r.text,
            )
            stored = connection.execute(
                "SELECT * FROM data_news WHERE data_version = ? AND symbol = ? AND record_id = ?",
                (data_version, r.symbol, r.record_id),
            ).fetchone()
            if stored is None:
                connection.execute("INSERT INTO data_news VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
                added += 1
            elif tuple(stored) != row:
                raise NewsConflict(
                    f"{r.symbol} article {r.record_id} is already stored under {data_version} "
                    "with different content. Import into a new data version."
                )
    return added


def _imported(connection: sqlite3.Connection) -> bool:
    """Whether any news was ever imported, so a missing table reads as missing, not as a 500."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'data_news_coverage'"
    ).fetchone()
    return row is not None


class SqliteNewsArchive:
    """Reads one news version. Every read takes the cutoff from the caller's trusted clock."""

    def __init__(self, database_path: Path, data_version: str = NEWS_VERSION) -> None:
        self.database_path = Path(database_path)
        self.data_version = data_version
        self.source = NEWS_SOURCE

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def visible(
        self, symbol: str, start_at: datetime, end_at: datetime, cutoff: datetime
    ) -> list[NewsRecord]:
        """Every article published in [start_at, end_at] and available by the cutoff.

        Raises `MissingCoverage` unless the fetched window covers the whole request, and
        `FutureDataError` when end_at is after the cutoff. Paging is the caller's job.
        """
        if end_at > cutoff:
            raise FutureDataError(f"end_at {end_at.isoformat()} is after the cutoff")
        with closing(self._connect()) as connection:
            if not _imported(connection):
                raise MissingCoverage(f"no news is imported under {self.data_version}")
            window = connection.execute(
                "SELECT start_at, end_at FROM data_news_coverage "
                "WHERE data_version = ? AND symbol = ?",
                (self.data_version, symbol),
            ).fetchone()
            if window is None or not (
                window["start_at"] <= stored_time(start_at)
                and stored_time(end_at) <= window["end_at"]
            ):
                raise MissingCoverage(f"news for {symbol} was not fetched for the whole window")
            rows = connection.execute(
                "SELECT * FROM data_news WHERE data_version = ? AND symbol = ? "
                "AND published_at >= ? AND published_at <= ? AND available_at <= ? "
                "ORDER BY published_at, record_id",
                (
                    self.data_version,
                    symbol,
                    stored_time(start_at),
                    stored_time(end_at),
                    stored_time(cutoff),
                ),
            ).fetchall()
        return [
            NewsRecord(
                symbol=r["symbol"],
                record_id=r["record_id"],
                revision=r["revision"],
                published_at=datetime.fromisoformat(r["published_at"]),
                available_at=datetime.fromisoformat(r["available_at"]),
                headline=r["headline"],
                text=r["text"],
            )
            for r in rows
        ]

"""Company filings: storage and reads cut off at a trusted simulated time.

Only 10-K and 10-Q filings and their amendments are served, each with the fiscal period its
own XBRL facts report. 8-K filings have no fiscal period and are not served. A filing is
published, revised and available at its EDGAR acceptance time. Each company is covered for
acceptances inside the window its documents were fetched for; a request outside it is missing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .archive import FutureDataError, MissingCoverage, stored_time

FILINGS_VERSION = "edgar-filings-v1"
FILINGS_SOURCE = "sec-edgar"
TEXT_LIMIT = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_filings (
    data_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    accession TEXT NOT NULL,
    form TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol, accession)
);
CREATE TABLE IF NOT EXISTS data_filings_coverage (
    data_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    cik INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol)
);
"""


class FilingConflict(Exception):
    """An import would change a filing or coverage already stored under the same version."""


@dataclass(frozen=True)
class FilingRecord:
    symbol: str
    accession: str
    form: str
    accepted_at: datetime
    period_start: date
    period_end: date
    text: str


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def import_filings(
    connection: sqlite3.Connection,
    records: Iterable[FilingRecord],
    companies: dict[str, int],
    window: tuple[datetime, datetime],
    *,
    data_version: str = FILINGS_VERSION,
) -> int:
    """Store filings and mark each company as covered for acceptances in `window`.

    `companies` maps each symbol whose full filing history was loaded to its CIK. Re-importing
    is a no-op. A changed filing or coverage raises `FilingConflict`, and nothing is written.
    Returns the number of filings newly stored.
    """
    ensure_schema(connection)
    added = 0
    with connection:
        for symbol, cik in companies.items():
            coverage = (data_version, symbol, cik, stored_time(window[0]), stored_time(window[1]))
            stored = connection.execute(
                "SELECT * FROM data_filings_coverage WHERE data_version = ? AND symbol = ?",
                (data_version, symbol),
            ).fetchone()
            if stored is None:
                connection.execute(
                    "INSERT INTO data_filings_coverage VALUES (?, ?, ?, ?, ?)", coverage
                )
            elif tuple(stored) != coverage:
                raise FilingConflict(f"{symbol} already has different coverage in {data_version}")
        for r in records:
            if r.symbol not in companies:
                raise FilingConflict(f"{r.symbol} has filings but no loaded history")
            row = (
                data_version,
                r.symbol,
                r.accession,
                r.form,
                stored_time(r.accepted_at),
                r.period_start.isoformat(),
                r.period_end.isoformat(),
                r.text,
            )
            stored = connection.execute(
                "SELECT * FROM data_filings WHERE data_version = ? AND symbol = ? AND accession = ?",
                (data_version, r.symbol, r.accession),
            ).fetchone()
            if stored is None:
                connection.execute("INSERT INTO data_filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
                added += 1
            elif tuple(stored) != row:
                raise FilingConflict(
                    f"{r.symbol} filing {r.accession} is already stored under {data_version} "
                    "with different content. Import into a new data version."
                )
    return added


class SqliteFilingArchive:
    """Reads one filings version. Every read takes the cutoff from the caller's trusted clock."""

    def __init__(self, database_path: Path, data_version: str = FILINGS_VERSION) -> None:
        self.database_path = Path(database_path)
        self.data_version = data_version
        self.source = FILINGS_SOURCE

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def visible(
        self, symbol: str, start_at: datetime, end_at: datetime, cutoff: datetime
    ) -> list[FilingRecord]:
        """Every filing accepted in [start_at, end_at] and by the cutoff, oldest first.

        Raises `MissingCoverage` unless the company was imported and its covered acceptance
        window holds the whole request, and `FutureDataError` when end_at is after the cutoff.
        """
        if end_at > cutoff:
            raise FutureDataError(f"end_at {end_at.isoformat()} is after the cutoff")
        with closing(self._connect()) as connection:
            imported = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'data_filings_coverage'"
            ).fetchone()
            if imported is None:
                raise MissingCoverage(f"no filings are imported under {self.data_version}")
            covered = connection.execute(
                "SELECT start_at, end_at FROM data_filings_coverage "
                "WHERE data_version = ? AND symbol = ?",
                (self.data_version, symbol),
            ).fetchone()
            if covered is None or not (
                covered["start_at"] <= stored_time(start_at)
                and stored_time(end_at) <= covered["end_at"]
            ):
                raise MissingCoverage(f"filings for {symbol} do not cover the whole window")
            rows = connection.execute(
                "SELECT * FROM data_filings WHERE data_version = ? AND symbol = ? "
                "AND accepted_at >= ? AND accepted_at <= ? AND accepted_at <= ? "
                "ORDER BY accepted_at, accession",
                (
                    self.data_version,
                    symbol,
                    stored_time(start_at),
                    stored_time(end_at),
                    stored_time(cutoff),
                ),
            ).fetchall()
        return [
            FilingRecord(
                symbol=r["symbol"],
                accession=r["accession"],
                form=r["form"],
                accepted_at=datetime.fromisoformat(r["accepted_at"]),
                period_start=date.fromisoformat(r["period_start"]),
                period_end=date.fromisoformat(r["period_end"]),
                text=r["text"],
            )
            for r in rows
        ]

    def latest_period_ends(self, symbols: list[str], cutoff: datetime) -> dict[str, date]:
        """For each symbol, the latest fiscal period end among filings accepted by the cutoff.

        A symbol with no such filing is absent. Raises `MissingCoverage` when no filings were
        ever imported. The maximum, not the most recent filing's period, so a late amendment
        for an older period never moves a company's cycle backwards.
        """
        with closing(self._connect()) as connection:
            imported = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'data_filings'"
            ).fetchone()
            if imported is None:
                raise MissingCoverage(f"no filings are imported under {self.data_version}")
            marks = ", ".join("?" for _ in symbols)
            rows = connection.execute(
                "SELECT symbol, MAX(period_end) AS period_end FROM data_filings "
                f"WHERE data_version = ? AND accepted_at <= ? AND symbol IN ({marks}) "
                "GROUP BY symbol",
                (self.data_version, stored_time(cutoff), *symbols),
            ).fetchall()
        return {r["symbol"]: date.fromisoformat(r["period_end"]) for r in rows}

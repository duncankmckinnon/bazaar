"""Daily price bars: storage, import, and reads cut off at a trusted simulated time.

A daily bar is observed and becomes available at its session's 16:00 Eastern close, so a read
cut off during a session sees the previous close and never the one still forming.

    uv run python -m bazaar_market.prices synthetic --out data/bars-synthetic-v1.csv
    uv run python -m bazaar_market.prices import data/bars-synthetic-v1.csv --db data/market.sqlite3
"""

from __future__ import annotations

import argparse
import csv
import random
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from bazaar_protocol import PriceObservation

EASTERN = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
CSV_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume")

SYNTHETIC_VERSION = "synthetic-v1"
SYNTHETIC_SOURCE = "synthetic"
DEMO_START, DEMO_END = date(2025, 7, 1), date(2026, 9, 30)
DEMO_SYMBOLS = (
    "AAPL", "MSFT", "NVDA", "AMZN", "JPM", "XOM", "KO", "JNJ", "WMT", "META", "FISV", "K", "EA",
)  # fmt: skip
# Synthetic series follow the demo's ticker history: Fiserv traded as FI until 2025-11-10,
# and Kellanova and Electronic Arts stopped trading when they were acquired for cash.
RENAMED_FROM = {"FISV": ("FI", date(2025, 11, 11))}
LAST_SESSION = {"K": date(2025, 12, 10), "EA": date(2026, 8, 4)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_bars (
    data_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL CHECK (volume >= 0),
    source TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol, observed_at)
);
"""


class MissingData(LookupError):
    """No bar answers the question. Never read as a price of zero or as "nothing happened"."""


class FutureDataError(Exception):
    """A read asked for time after the trusted cutoff."""


class BarConflict(Exception):
    """An import would change a bar already stored under the same data version."""


@dataclass(frozen=True)
class Bar:
    symbol: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class TradingSession:
    day: date
    open_at: datetime
    close_at: datetime


def _eastern(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=EASTERN).astimezone(UTC)


def close_at(day: date) -> datetime:
    """The 16:00 Eastern close of `day`, in UTC."""
    return _eastern(day, SESSION_CLOSE)


def _stored(value: datetime) -> str:
    """One stored format, so stored instants compare correctly as strings."""
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def import_bars(
    connection: sqlite3.Connection, bars: Iterable[Bar], *, data_version: str, source: str
) -> int:
    """Store bars under `data_version`. Re-importing the same bars is a no-op.

    A bar that differs from the one already stored raises `BarConflict`, and nothing is written.
    Returns the number of bars newly stored.
    """
    ensure_schema(connection)
    added = 0
    with connection:
        for bar in bars:
            if not all(p > 0 and p.is_finite() for p in (bar.open, bar.high, bar.low, bar.close)):
                raise ValueError(f"{bar.symbol} {bar.session}: prices must be positive")
            observed = _stored(close_at(bar.session))
            row = (
                data_version,
                bar.symbol,
                observed,
                observed,
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                bar.volume,
                source,
            )
            stored = connection.execute(
                "SELECT * FROM data_bars WHERE data_version = ? AND symbol = ? AND observed_at = ?",
                (data_version, bar.symbol, observed),
            ).fetchone()
            if stored is None:
                connection.execute(
                    "INSERT INTO data_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row
                )
                added += 1
            elif tuple(stored) != row:
                raise BarConflict(
                    f"{bar.symbol} {bar.session} is already stored under {data_version} with "
                    "different values. Import into a new data version."
                )
    return added


def read_bars_csv(path: Path) -> list[Bar]:
    """Read the bar CSV format: symbol, date (YYYY-MM-DD), open, high, low, close, volume."""
    with Path(path).open(newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise ValueError(f"{path} must have the columns {', '.join(CSV_COLUMNS)}")
        return [
            Bar(
                symbol=row["symbol"],
                session=date.fromisoformat(row["date"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=int(row["volume"]),
            )
            for row in reader
        ]


def write_bars_csv(path: Path, bars: Iterable[Bar]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for b in bars:
            writer.writerow((b.symbol, b.session, b.open, b.high, b.low, b.close, b.volume))


def synthetic_bars(
    symbols: Iterable[str] = DEMO_SYMBOLS, start: date = DEMO_START, end: date = DEMO_END
) -> list[Bar]:
    """A seeded random walk per symbol on every weekday. The same arguments give the same bars."""
    cent = Decimal("0.01")
    bars = []
    for symbol in symbols:
        rng = random.Random(f"{SYNTHETIC_VERSION}:{symbol}")
        close = rng.uniform(30, 400)
        day = start
        while day <= end and day <= LAST_SESSION.get(symbol, end):
            if day.weekday() < 5:
                open_ = close * (1 + rng.gauss(0, 0.004))
                close = open_ * (1 + rng.gauss(0.0003, 0.015))
                high = max(open_, close) * (1 + abs(rng.gauss(0, 0.005)))
                low = min(open_, close) * (1 - abs(rng.gauss(0, 0.005)))
                old_ticker, renamed_on = RENAMED_FROM.get(symbol, (symbol, start))
                bars.append(
                    Bar(
                        symbol=old_ticker if day < renamed_on else symbol,
                        session=day,
                        open=Decimal(open_).quantize(cent),
                        high=Decimal(high).quantize(cent),
                        low=Decimal(low).quantize(cent),
                        close=Decimal(close).quantize(cent),
                        volume=rng.randint(1_000_000, 50_000_000),
                    )
                )
            day += timedelta(days=1)
    return bars


def _observation(row: sqlite3.Row) -> PriceObservation:
    return PriceObservation(
        observed_at=datetime.fromisoformat(row["observed_at"]),
        available_at=datetime.fromisoformat(row["available_at"]),
        price=Decimal(row["close"]),
    )


class SqliteMarketData:
    """Reads one data version. Every read takes the cutoff from the caller's trusted clock."""

    def __init__(self, database_path: Path, data_version: str) -> None:
        self.database_path = Path(database_path)
        self.data_version = data_version

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @property
    def price_source(self) -> str:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT source FROM data_bars WHERE data_version = ?",
                (self.data_version,),
            ).fetchall()
        if len(rows) != 1:
            raise MissingData(
                f"{self.data_version} needs exactly one price source, has {len(rows)}"
            )
        return rows[0]["source"]

    def _require_symbol(self, connection: sqlite3.Connection, symbol: str) -> None:
        known = connection.execute(
            "SELECT 1 FROM data_bars WHERE data_version = ? AND symbol = ? LIMIT 1",
            (self.data_version, symbol),
        ).fetchone()
        if known is None:
            raise MissingData(f"no prices for {symbol} in {self.data_version}")

    def session(self, day: date) -> TradingSession | None:
        """The session on `day`, or None when no symbol has a bar that day (market closed)."""
        with closing(self._connect()) as connection:
            traded = connection.execute(
                "SELECT 1 FROM data_bars WHERE data_version = ? AND observed_at = ? LIMIT 1",
                (self.data_version, _stored(close_at(day))),
            ).fetchone()
        if traded is None:
            return None
        return TradingSession(day=day, open_at=_eastern(day, SESSION_OPEN), close_at=close_at(day))

    def price_at(self, symbol: str, cutoff: datetime) -> PriceObservation:
        """The latest close available at or before `cutoff`. Raises `MissingData` if none is."""
        with closing(self._connect()) as connection:
            self._require_symbol(connection, symbol)
            row = connection.execute(
                "SELECT * FROM data_bars WHERE data_version = ? AND symbol = ? "
                "AND available_at <= ? ORDER BY observed_at DESC LIMIT 1",
                (self.data_version, symbol, _stored(cutoff)),
            ).fetchone()
        if row is None:
            raise MissingData(f"no price for {symbol} available by {cutoff.isoformat()}")
        return _observation(row)

    def price_history(
        self, symbol: str, start_at: datetime, end_at: datetime, cutoff: datetime, limit: int
    ) -> tuple[PriceObservation, ...]:
        """Closes observed in [start_at, end_at], oldest first, at most `limit` of them."""
        if end_at > cutoff:
            raise FutureDataError(f"end_at {end_at.isoformat()} is after the cutoff")
        with closing(self._connect()) as connection:
            self._require_symbol(connection, symbol)
            rows = connection.execute(
                "SELECT * FROM data_bars WHERE data_version = ? AND symbol = ? "
                "AND observed_at >= ? AND observed_at <= ? AND available_at <= ? "
                "ORDER BY observed_at LIMIT ?",
                (
                    self.data_version,
                    symbol,
                    _stored(start_at),
                    _stored(end_at),
                    _stored(cutoff),
                    limit,
                ),
            ).fetchall()
        return tuple(_observation(r) for r in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bazaar_market.prices", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    synthetic = commands.add_parser("synthetic", help="write the synthetic-v1 bar CSV")
    synthetic.add_argument("--out", type=Path, required=True)
    load = commands.add_parser("import", help="import a bar CSV into the market database")
    load.add_argument("csv", type=Path)
    load.add_argument("--db", type=Path, required=True)
    load.add_argument("--data-version", default=SYNTHETIC_VERSION)
    load.add_argument("--source", default=SYNTHETIC_SOURCE)
    args = parser.parse_args(argv)

    if args.command == "synthetic":
        bars = synthetic_bars()
        write_bars_csv(args.out, bars)
        print(f"wrote {len(bars)} synthetic bars to {args.out}")
    else:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(args.db)) as connection:
            added = import_bars(
                connection,
                read_bars_csv(args.csv),
                data_version=args.data_version,
                source=args.source,
            )
        print(f"imported {added} new bars into {args.db} as {args.data_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

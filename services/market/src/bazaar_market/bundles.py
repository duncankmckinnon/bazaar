"""Data bundles: one experiment-facing data_version naming a bars, news and filings version.

An experiment's data_version is either a bundle id or, for backward compatibility, a plain bars
version (alpaca-bars-v1, synthetic-v1), which has bars only. Pages and items report the
experiment's data_version; the component version goes in their source.

A demo bundle also has a window (start and end dates, holidays) and a universe: data_universe
rows of symbol, name and GICS sector, plus at most one benchmark row. Its sessions are the dates
in the window on which every universe symbol and the benchmark have a bar. A date where only some
do is an incomplete session, reported so that consumers fail loudly rather than trade around a
hole. demo-bundle-v1's universe is AAPL, KO and MSFT, seeded here, with no benchmark.
demo-bundle-v2 is the S&P 500 as of 2026-09-25 with SPY as the benchmark; the data importer
stores its rows with `store_universe` after `seed`.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from bazaar_market.prices import EASTERN, close_at

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_bundles (
    bundle_id TEXT PRIMARY KEY,
    bars TEXT NOT NULL,
    news TEXT NOT NULL,
    filings TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS data_bundles_immutable_update BEFORE UPDATE ON data_bundles
BEGIN SELECT RAISE(ABORT, 'bundles are immutable'); END;
CREATE TRIGGER IF NOT EXISTS data_bundles_immutable_delete BEFORE DELETE ON data_bundles
BEGIN SELECT RAISE(ABORT, 'bundles are immutable'); END;
CREATE TABLE IF NOT EXISTS data_bundle_windows (
    bundle_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    holidays TEXT NOT NULL,
    universe_name TEXT,
    benchmark TEXT,
    CHECK (start_date <= end_date)
);
CREATE TRIGGER IF NOT EXISTS data_bundle_windows_immutable_update
BEFORE UPDATE ON data_bundle_windows
BEGIN SELECT RAISE(ABORT, 'bundle windows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS data_bundle_windows_immutable_delete
BEFORE DELETE ON data_bundle_windows
BEGIN SELECT RAISE(ABORT, 'bundle windows are immutable'); END;
CREATE TABLE IF NOT EXISTS data_universe (
    bundle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    sector TEXT NOT NULL,
    cik TEXT,
    benchmark INTEGER NOT NULL CHECK (benchmark IN (0, 1)),
    PRIMARY KEY (bundle_id, symbol)
);
CREATE TRIGGER IF NOT EXISTS data_universe_immutable_update BEFORE UPDATE ON data_universe
BEGIN SELECT RAISE(ABORT, 'universes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS data_universe_immutable_delete BEFORE DELETE ON data_universe
BEGIN SELECT RAISE(ABORT, 'universes are immutable'); END;
"""

BENCHMARK_SECTOR = "Benchmark"
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")  # bazaar_protocol.Symbol

Kind = Literal["bars", "news", "filings"]

DEMO_BUNDLES: dict[str, dict[Kind, str]] = {
    "demo-bundle-v1": {
        "bars": "alpaca-bars-v1",
        "news": "alpaca-news-v1",
        "filings": "edgar-filings-v1",
    },
    "demo-bundle-v2": {
        "bars": "alpaca-bars-v2",
        "news": "alpaca-news-v2",
        "filings": "edgar-filings-v2",
    },
}


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    holidays: tuple[date, ...] = ()
    universe_name: str | None = None
    benchmark: str | None = None

    def expected_sessions(self) -> list[date]:
        """Weekdays in the window that are not exchange holidays. Never derived from the data."""
        days, day = [], self.start
        while day <= self.end:
            if day.weekday() < 5 and day not in self.holidays:
                days.append(day)
            day += timedelta(days=1)
        return days


# NYSE holidays inside each window: none (Presidents' Day 2026 is 02-16, after v1's window).
DEMO_WINDOWS: dict[str, Window] = {
    "demo-bundle-v1": Window(date(2026, 2, 2), date(2026, 2, 13)),
    "demo-bundle-v2": Window(
        date(2026, 9, 28), date(2026, 10, 9), universe_name="S&P 500", benchmark="SPY"
    ),
}


@dataclass(frozen=True)
class Member:
    symbol: str
    name: str
    sector: str
    cik: str | None = None
    benchmark: bool = False


# demo-bundle-v2's universe is imported from config/sp500-2026-09-25.csv, not seeded here.
DEMO_UNIVERSES: dict[str, tuple[Member, ...]] = {
    "demo-bundle-v1": (
        Member("AAPL", "Apple Inc.", "Information Technology", "320193"),
        Member("KO", "Coca-Cola Company (The)", "Consumer Staples", "21344"),
        Member("MSFT", "Microsoft", "Information Technology", "789019"),
    ),
}


class NoComponent(LookupError):
    """The experiment's data_version has no version of this kind. Treat as missing coverage."""


class BundleConflict(Exception):
    """A bundle id is already stored with different components or a different window."""


def seed(
    connection: sqlite3.Connection, bundles: dict[str, dict[Kind, str]] = DEMO_BUNDLES
) -> None:
    """Store `bundles`. Re-seeding the same bundles is a no-op; a changed bundle is refused."""
    for bundle_id, parts in bundles.items():
        connection.execute(
            "INSERT OR IGNORE INTO data_bundles VALUES (?, ?, ?, ?)",
            (bundle_id, parts["bars"], parts["news"], parts["filings"]),
        )
        row = connection.execute(
            "SELECT bars, news, filings FROM data_bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        if tuple(row) != (parts["bars"], parts["news"], parts["filings"]):
            raise BundleConflict(f"{bundle_id} is already stored with different components")
    for bundle_id, window in DEMO_WINDOWS.items():
        stored = (
            window.start.isoformat(),
            window.end.isoformat(),
            ",".join(d.isoformat() for d in sorted(window.holidays)),
            window.universe_name,
            window.benchmark,
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(data_bundle_windows)")}
        if "universe_name" not in columns:
            raise BundleConflict(
                "data_bundle_windows predates universes (v5); recreate this database"
            )
        connection.execute(
            "INSERT OR IGNORE INTO data_bundle_windows VALUES (?, ?, ?, ?, ?, ?)",
            (bundle_id, *stored),
        )
        row = connection.execute(
            "SELECT start_date, end_date, holidays, universe_name, benchmark "
            "FROM data_bundle_windows WHERE bundle_id = ?",
            (bundle_id,),
        ).fetchone()
        if tuple(row) != stored:
            raise BundleConflict(f"{bundle_id} is already stored with a different window")
    for bundle_id, members in DEMO_UNIVERSES.items():
        store_universe(connection, bundle_id, members)


def store_universe(
    connection: sqlite3.Connection, bundle_id: str, members: tuple[Member, ...] | list[Member]
) -> None:
    """Store a bundle's universe once. Its window must be seeded first. Storing the same rows
    again is a no-op; any changed, added or missing row raises BundleConflict.

    The benchmark is one row with benchmark=True and sector "Benchmark", and must be the
    window's benchmark; a bundle whose window has none stores no benchmark row.
    """
    stored_window = window(connection, bundle_id)
    if stored_window is None:
        raise BundleConflict(f"{bundle_id} has no window; seed the bundles first")
    rows = sorted((m.symbol, m.name, m.sector, m.cik, int(m.benchmark)) for m in members)
    problems = [m.symbol for m in members if not SYMBOL.match(m.symbol)]
    problems += [m.symbol for m in members if not m.name.strip() or not m.sector.strip()]
    problems += [m.symbol for m in members if m.benchmark != (m.sector == BENCHMARK_SECTOR)]
    if problems:
        raise BundleConflict(f"{bundle_id}: invalid universe rows {sorted(set(problems))}")
    if len({row[0] for row in rows}) != len(rows):
        raise BundleConflict(f"{bundle_id}: a symbol appears twice in the universe")
    benchmarks = [m.symbol for m in members if m.benchmark]
    expected = [stored_window.benchmark] if stored_window.benchmark else []
    if benchmarks != expected or not rows or len(benchmarks) == len(rows):
        raise BundleConflict(
            f"{bundle_id}: the universe needs constituents and benchmark rows {expected}, "
            f"got {benchmarks}"
        )
    existing = sorted(
        tuple(row)
        for row in connection.execute(
            "SELECT symbol, name, sector, cik, benchmark FROM data_universe WHERE bundle_id = ?",
            (bundle_id,),
        )
    )
    if existing == rows:
        return
    if existing:
        raise BundleConflict(f"{bundle_id} is already stored with a different universe")
    connection.executemany(
        "INSERT INTO data_universe VALUES (?, ?, ?, ?, ?, ?)",
        [(bundle_id, *row) for row in rows],
    )


def universe(connection: sqlite3.Connection, bundle_id: str) -> tuple[Member, ...]:
    """The bundle's universe, benchmark included, by symbol. Empty when none is stored."""
    rows = connection.execute(
        "SELECT symbol, name, sector, cik, benchmark FROM data_universe WHERE bundle_id = ? "
        "ORDER BY symbol",
        (bundle_id,),
    ).fetchall()
    return tuple(Member(s, n, sector, cik, bool(b)) for s, n, sector, cik, b in rows)


def component(connection: sqlite3.Connection, data_version: str, kind: Kind) -> str:
    if kind not in ("bars", "news", "filings"):
        raise ValueError(f"unknown component kind {kind!r}")
    row = connection.execute(
        "SELECT bars, news, filings FROM data_bundles WHERE bundle_id = ?", (data_version,)
    ).fetchone()
    if row is not None:
        return dict(zip(("bars", "news", "filings"), row, strict=True))[kind]
    if kind == "bars":
        return data_version
    raise NoComponent(f"{data_version} has no {kind} version")


def window(connection: sqlite3.Connection, bundle_id: str) -> Window | None:
    row = connection.execute(
        "SELECT start_date, end_date, holidays, universe_name, benchmark "
        "FROM data_bundle_windows WHERE bundle_id = ?",
        (bundle_id,),
    ).fetchone()
    if row is None:
        return None
    start, end, holidays, universe_name, benchmark = row
    return Window(
        date.fromisoformat(start),
        date.fromisoformat(end),
        tuple(date.fromisoformat(d) for d in holidays.split(",") if d),
        universe_name,
        benchmark,
    )


@dataclass(frozen=True)
class Sessions:
    expected: list[date]
    complete: list[date]
    incomplete: list[tuple[date, tuple[str, ...]]]  # (expected session, symbols with no bar)
    bars: dict[str, list[date]]  # each symbol's bar dates in the window


def sessions(
    connection: sqlite3.Connection, bars_version: str, window: Window, symbols: list[str]
) -> Sessions:
    """Check every expected session of the window: complete if every one of `symbols` (the
    universe and the benchmark) has a bar that New York date, otherwise incomplete with the
    symbols missing. A day with no bars at all is incomplete with every symbol missing, so it
    cannot vanish.
    """
    marks = ",".join("?" * len(symbols))
    rows = connection.execute(
        f"SELECT symbol, observed_at FROM data_bars WHERE data_version = ? AND symbol IN ({marks}) "
        "AND observed_at >= ? AND observed_at <= ?",
        (bars_version, *symbols, close_at(window.start).isoformat(timespec="microseconds"),
         close_at(window.end).isoformat(timespec="microseconds")),
    ).fetchall()  # fmt: skip
    bars: dict[str, list[date]] = {symbol: [] for symbol in symbols}
    for symbol, observed_at in rows:
        day = datetime.fromisoformat(observed_at).astimezone(EASTERN).date()
        if window.start <= day <= window.end:
            bars[symbol].append(day)
    expected = window.expected_sessions()
    complete, incomplete = [], []
    for day in expected:
        missing = tuple(s for s in sorted(symbols) if day not in bars[s])
        if missing:
            incomplete.append((day, missing))
        else:
            complete.append(day)
    return Sessions(expected, complete, incomplete, {s: sorted(d) for s, d in bars.items()})

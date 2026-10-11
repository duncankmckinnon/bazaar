"""Store a bundle's universe from the universe CSV, keeping only symbols the bars fully cover.

The CSV is the true as-of constituent list. A constituent qualifies for the bundle only if it has
a bar on every session from `required_from` to `required_to` (the window plus the lookback its
first day needs). The sessions are the days the benchmark has a bar, and the benchmark itself must
cover every weekday of that range. Every other constituent is excluded, never filled, and recorded
in data_universe_exclusions with the reason and its last bar.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .. import bundles, db
from .bars_import import weekdays
from .errors import SourceError
from .universe import UniverseRow

EXCLUSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_universe_exclusions (
    bundle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reason TEXT NOT NULL,
    last_bar TEXT,
    PRIMARY KEY (bundle_id, symbol)
);
"""


# The benchmark's display name. A benchmark not listed here is refused rather than named by guess.
BENCHMARK_NAMES = {"SPY": "SPDR S&P 500 ETF Trust"}


@dataclass(frozen=True)
class Exclusion:
    symbol: str
    reason: str
    last_bar: date | None


@dataclass(frozen=True)
class UniverseReport:
    members: int
    sessions: tuple[date, ...]
    excluded: tuple[Exclusion, ...]


def _bar_days(connection: sqlite3.Connection, data_version: str) -> dict[str, list[date]]:
    days: dict[str, list[date]] = {}
    rows = connection.execute(
        "SELECT symbol, observed_at FROM data_bars WHERE data_version = ? ORDER BY observed_at",
        (data_version,),
    )
    for symbol, observed_at in rows:
        # observed_at is the 16:00 New York close in UTC, so its UTC date is the session date.
        days.setdefault(symbol, []).append(date.fromisoformat(observed_at[:10]))
    return days


def import_universe(
    database_path: Path,
    bundle_id: str,
    rows: list[UniverseRow],
    *,
    benchmark: str,
    benchmark_name: str,
    bars_version: str,
    required_from: date,
    required_to: date,
) -> UniverseReport:
    """Seed the bundles, then store the qualifying universe and the exclusions in one transaction."""
    db.initialize(database_path, bundles.SCHEMA, EXCLUSIONS_SCHEMA)
    with db.write_transaction(database_path, operation="import_universe") as connection:
        days = _bar_days(connection, bars_version)
        held = set(days.get(benchmark, ()))
        missing = sorted(weekdays(required_from, required_to) - held)
        if missing:
            raise SourceError(
                f"the benchmark {benchmark} has no {bars_version} bar on "
                f"{', '.join(map(str, missing))}"
            )
        sessions = tuple(sorted(d for d in held if required_from <= d <= required_to))
        members, excluded = [], []
        for row in rows:
            own = days.get(row.symbol, [])
            have = set(own)
            gaps = [d for d in sessions if d not in have]
            if not own:
                excluded.append(Exclusion(row.symbol, f"no {bars_version} bars", None))
            elif gaps:
                reason = f"no bar on {', '.join(map(str, gaps))}"
                excluded.append(Exclusion(row.symbol, reason, own[-1]))
            else:
                members.append(bundles.Member(row.symbol, row.name, row.sector, str(row.cik)))
        members.append(
            bundles.Member(benchmark, benchmark_name, bundles.BENCHMARK_SECTOR, None, True)
        )
        bundles.seed(connection)
        bundles.store_universe(connection, bundle_id, members)
        _store_exclusions(connection, bundle_id, excluded)
    return UniverseReport(len(members) - 1, sessions, tuple(excluded))


def _store_exclusions(
    connection: sqlite3.Connection, bundle_id: str, excluded: list[Exclusion]
) -> None:
    rows = sorted(
        (e.symbol, e.reason, e.last_bar.isoformat() if e.last_bar else None) for e in excluded
    )
    existing = sorted(
        tuple(r)
        for r in connection.execute(
            "SELECT symbol, reason, last_bar FROM data_universe_exclusions WHERE bundle_id = ?",
            (bundle_id,),
        )
    )
    if existing == rows:
        return
    if existing:
        raise bundles.BundleConflict(f"{bundle_id} is already stored with different exclusions")
    connection.executemany(
        "INSERT INTO data_universe_exclusions VALUES (?, ?, ?, ?)",
        [(bundle_id, *r) for r in rows],
    )

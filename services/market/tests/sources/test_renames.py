"""A company renamed inside the window keeps its universe symbol (PSKY -> SKYD on 2026-10-06)."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
from bazaar_market.prices import SqliteMarketData, close_at
from bazaar_market.sources.bars_import import import_bars_snapshot
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.universe import Rename

from .test_alpaca_bars import freeze, payload

DAYS = [date(2026, 9, 21) + timedelta(days=n) for n in range(19)]
DAYS = [d for d in DAYS if d.weekday() < 5]  # 2026-09-21 to 2026-10-09: 15 sessions
WINDOW = [d for d in DAYS if d >= date(2026, 9, 28)]
RENAMED = date(2026, 10, 6)
SKYD = Rename("SKYD", "PSKY", RENAMED)
OLD_LISTING = date(2026, 9, 22)  # an earlier, unrelated SKYD listing


def snapshot(tmp_path, *, psky_days=None, skyd_days=None):
    psky = [d for d in DAYS if d < RENAMED] if psky_days is None else psky_days
    skyd = [OLD_LISTING, *(d for d in DAYS if d >= RENAMED)] if skyd_days is None else skyd_days
    pages = {"SPY": [payload("SPY", DAYS)], "PSKY": [payload("PSKY", psky)]}
    pages["SKYD"] = [payload("SKYD", skyd)]
    return freeze(tmp_path, pages, start=DAYS[0], end=DAYS[-1])


def run(tmp_path, snap, renames=(SKYD,)):
    db = tmp_path / "m.db"
    required = {"PSKY": set(DAYS), "SPY": set(DAYS)}
    with closing(sqlite3.connect(db)) as connection:
        report = import_bars_snapshot(
            connection, snap.dir, data_version="alpaca-bars-v2", required=required, renames=renames
        )
    return db, report


def test_the_renamed_tickers_bars_are_stored_under_the_universe_symbol(tmp_path):
    db, report = run(tmp_path, snapshot(tmp_path))

    with closing(sqlite3.connect(db)) as connection:
        symbols = {r[0] for r in connection.execute("SELECT DISTINCT symbol FROM data_bars")}
        changes = connection.execute("SELECT * FROM data_ticker_changes").fetchall()
    assert symbols == {"PSKY", "SPY"}  # no SKYD row for any market route to serve
    assert changes == [("alpaca-bars-v2", "SKYD", "PSKY", "2026-10-06")]
    market = SqliteMarketData(db, "alpaca-bars-v2")
    assert all(market.price_at("PSKY", close_at(d)).observed_at == close_at(d) for d in WINDOW)
    assert len(WINDOW) == 10
    [applied] = report.renamed
    assert (applied.mapped, applied.dropped_before) == (4, 1)
    assert [c.ticker for c in report.coverage] == ["PSKY", "SPY"]


def test_without_the_rename_psky_misses_the_last_four_sessions(tmp_path):
    with pytest.raises(SourceError, match="PSKY is required on 2026-10-06, 2026-10-07"):
        run(tmp_path, snapshot(tmp_path), renames=())


def test_a_wrong_rename_date_that_overlaps_the_old_ticker_fails(tmp_path):
    snap = snapshot(tmp_path, psky_days=DAYS)  # PSKY still trades after the claimed rename

    with pytest.raises(SourceError, match="rename date is wrong"):
        run(tmp_path, snap)


def test_a_rename_date_too_late_leaves_a_gap_the_required_check_catches(tmp_path):
    late = Rename("SKYD", "PSKY", date(2026, 10, 7))

    with pytest.raises(SourceError, match="PSKY is required on 2026-10-06"):
        run(tmp_path, snapshot(tmp_path), renames=(late,))

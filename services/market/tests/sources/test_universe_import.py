"""import-universe keeps only constituents with a bar on every required session."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

import pytest
from bazaar_market import bundles, db
from bazaar_market.prices import Bar, import_bars
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.universe import UniverseRow
from bazaar_market.sources.universe_import import import_universe

DAYS = [date(2026, 9, 21) + timedelta(days=n) for n in range(19)]
DAYS = [d for d in DAYS if d.weekday() < 5]  # 2026-09-21 to 2026-10-09: 15 sessions
ROWS = [
    UniverseRow("AAPL", "Apple Inc.", "Information Technology", 320193),
    UniverseRow("BRK.B", "Berkshire Hathaway", "Financials", 1067983),
    UniverseRow("WBD", "Warner Bros. Discovery", "Communication Services", 1437107),
    UniverseRow("NEW", "Never Traded", "Industrials", 1),
]


def bars(symbol, days):
    one = Decimal(10)
    return [Bar(symbol, d, one, one, one, one, 100) for d in days]


def market(tmp_path, spy_days=DAYS):
    path = tmp_path / "market.db"
    with closing(sqlite3.connect(path)) as connection:
        stock = [*bars("AAPL", DAYS), *bars("BRK.B", DAYS), *bars("SPY", spy_days)]
        wbd = bars("WBD", [d for d in DAYS if d <= date(2026, 10, 5)])
        import_bars(connection, [*stock, *wbd], data_version="alpaca-bars-v2", source="t")
    return path


def run(path):
    return import_universe(
        path,
        "demo-bundle-v2",
        ROWS,
        benchmark="SPY",
        benchmark_name="SPDR S&P 500 ETF Trust",
        bars_version="alpaca-bars-v2",
        required_from=DAYS[0],
        required_to=DAYS[-1],
    )


def test_a_constituent_missing_a_session_is_excluded_with_its_reason_and_last_bar(tmp_path):
    path = market(tmp_path)

    report = run(path)

    assert report.members == 2 and len(report.sessions) == 15
    assert [(e.symbol, e.last_bar) for e in report.excluded] == [
        ("WBD", date(2026, 10, 5)),
        ("NEW", None),
    ]
    assert report.excluded[0].reason == "no bar on 2026-10-06, 2026-10-07, 2026-10-08, 2026-10-09"
    with closing(db.connect(path)) as connection:
        members = bundles.universe(connection, "demo-bundle-v2")
        exclusions = connection.execute(
            "SELECT symbol, last_bar FROM data_universe_exclusions ORDER BY symbol"
        ).fetchall()
    assert [(m.symbol, m.sector, m.cik, m.benchmark) for m in members] == [
        ("AAPL", "Information Technology", "320193", False),
        ("BRK.B", "Financials", "1067983", False),
        ("SPY", "Benchmark", None, True),
    ]
    assert [tuple(r) for r in exclusions] == [("NEW", None), ("WBD", "2026-10-05")]


def test_reimporting_the_same_universe_is_a_no_op(tmp_path):
    path = market(tmp_path)
    run(path)

    assert run(path).members == 2


def test_a_benchmark_missing_a_weekday_stops_everything(tmp_path):
    path = market(tmp_path, spy_days=[d for d in DAYS if d != date(2026, 9, 30)])

    with pytest.raises(SourceError, match="SPY has no alpaca-bars-v2 bar on 2026-09-30"):
        run(path)

    with closing(db.connect(path)) as connection:
        assert bundles.universe(connection, "demo-bundle-v2") == ()

"""Bundle windows, GET /bundles/{id} and the coverage report, on a synthetic v2 database."""

import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from bazaar_market import bundles, db, filings, news
from bazaar_market.app import create_app
from bazaar_market.coverage import main, report
from bazaar_market.prices import Bar, ensure_schema, import_bars
from bazaar_market.sources.bars_import import TICKER_CHANGES_SCHEMA
from bazaar_market.sources.universe_import import EXCLUSIONS_SCHEMA
from fastapi.testclient import TestClient

V2 = bundles.DEMO_WINDOWS["demo-bundle-v2"]
EXPECTED = V2.expected_sessions()
UNIVERSE = (
    bundles.Member("AAPL", "Apple Inc.", "Information Technology", "320193"),
    bundles.Member("KO", "Coca-Cola Company (The)", "Consumer Staples", "21344"),
    bundles.Member("MSFT", "Microsoft", "Information Technology", "789019"),
    bundles.Member("SPY", "SPDR S&P 500 ETF Trust", "Benchmark", None, benchmark=True),
)
SYMBOLS = tuple(m.symbol for m in UNIVERSE)  # the universe and the benchmark
WHOLE_DAY = date(2026, 10, 2)
PARTIAL_DAY = date(2026, 10, 6)


def at(day: date, hour: int) -> str:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC).isoformat(
        timespec="microseconds"
    )


def seed(
    path,
    *,
    missing_days: tuple[date, ...] = (),
    missing_bar: tuple[str, date] | None = None,
    stops_after: tuple[str, date] | None = None,
    news_coverage: str = "full",  # full | none | short
    filing_symbols: tuple[str, ...] | None = None,  # None: every symbol has a filing
) -> None:
    """A synthetic v2 database. By default it is FULL: every universe symbol has a bar on every
    expected session, news coverage spanning the window, and a filing with a visible period."""
    db.initialize(path, bundles.SCHEMA)
    with db.write_transaction(path) as connection:
        bundles.seed(connection)
        bundles.store_universe(connection, "demo-bundle-v2", UNIVERSE)
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        p = Decimal("100.00")
        bars = [
            Bar(symbol=s, session=d, open=p, high=p, low=p, close=p, volume=1000)
            for s in SYMBOLS
            for d in EXPECTED
            if d not in missing_days
            and (s, d) != missing_bar
            and not (stops_after and s == stops_after[0] and d > stops_after[1])
        ]
        import_bars(connection, bars, data_version="alpaca-bars-v2", source="synthetic")
        news.ensure_schema(connection)
        filings.ensure_schema(connection)
        if news_coverage != "none":
            end = date(2026, 10, 10) if news_coverage == "full" else date(2026, 10, 5)
            for s in SYMBOLS:
                connection.execute(
                    "INSERT INTO data_news VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("alpaca-news-v2", s, f"n-{s}", "r1", at(V2.start, 14), at(V2.start, 15),
                     "headline", ""),
                )  # fmt: skip
                connection.execute(
                    "INSERT INTO data_news_coverage VALUES (?, ?, ?, ?)",
                    ("alpaca-news-v2", s, at(date(2026, 9, 1), 0), at(end, 0)),
                )
        for s in SYMBOLS if filing_symbols is None else filing_symbols:
            connection.execute(
                "INSERT INTO data_filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("edgar-filings-v2", s, f"a-{s}", "10-Q", at(date(2026, 8, 1), 20),
                 "2026-03-29", "2026-06-27", "q"),
            )  # fmt: skip
        connection.execute(  # filed after the window: never visible by its end
            "INSERT INTO data_filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("edgar-filings-v2", "AAPL", "late", "10-K", at(date(2026, 10, 30), 20),
             "2025-09-28", "2026-09-26", "fy"),
        )  # fmt: skip
        connection.commit()


def failures_for(tmp_path, **kwargs) -> list[str]:
    seed(tmp_path / "m.sqlite3", **kwargs)
    return report(tmp_path / "m.sqlite3", "demo-bundle-v2")[1]


def test_the_v2_window_expects_ten_sessions():
    assert V2.holidays == () and len(EXPECTED) == 10
    assert EXPECTED[0] == date(2026, 9, 28) and EXPECTED[-1] == date(2026, 10, 9)
    v1 = bundles.DEMO_WINDOWS["demo-bundle-v1"]
    assert (v1.start, v1.end, v1.holidays, len(v1.expected_sessions())) == (
        date(2026, 2, 2), date(2026, 2, 13), (), 10,
    )  # fmt: skip


def test_a_full_database_passes(tmp_path, capsys):
    path = tmp_path / "m.sqlite3"
    seed(path)
    before = os.stat(path).st_mtime_ns
    assert main(["--db", str(path), "--bundle", "demo-bundle-v2"]) == 0
    out = capsys.readouterr().out
    aapl = next(line for line in out.splitlines() if line.startswith("AAPL"))
    assert "bars 10/10" in aapl and "news 1 (coverage 2026-09-01..2026-10-10)" in aapl
    assert "filings 1 visible, latest period end 2026-06-27, cycle start 2026-06-28" in aapl
    assert out.splitlines()[-5:] == [
        "summary:",
        "  bars: 4/4 symbols full, 0 with missing bars",
        "  news coverage spanning the window: 4/4",
        "  filings with a fiscal period end by the window end: 4/4",
        (
            "  ticker changes in the window: renames not recorded (no data_ticker_changes table); "
            "exclusions not recorded (no data_universe_exclusions table)"
        ),
    ]
    assert os.stat(path).st_mtime_ns == before  # read-only


def test_a_whole_missing_day_fails(tmp_path):
    failures = failures_for(tmp_path, missing_days=(WHOLE_DAY,))
    assert "2026-10-02: no bars for any symbol (whole day missing)" in failures


def test_a_partly_missing_day_fails(tmp_path):
    assert failures_for(tmp_path, missing_bar=("AAPL", PARTIAL_DAY)) == [
        "AAPL: no bar on 2026-10-06"
    ]


def test_a_missing_benchmark_bar_fails(tmp_path):
    assert failures_for(tmp_path, missing_bar=("SPY", PARTIAL_DAY)) == ["SPY: no bar on 2026-10-06"]


def test_bars_that_stop_inside_the_window_fail_and_show_as_a_ticker_change(tmp_path):
    seed(tmp_path / "m.sqlite3", stops_after=("KO", date(2026, 10, 5)))
    lines, failures = report(tmp_path / "m.sqlite3", "demo-bundle-v2")
    assert failures == ["KO: no bar on 2026-10-06, 2026-10-07, 2026-10-08, 2026-10-09"]
    assert lines[-5:-3] == [
        "summary:",
        "  bars: 3/4 symbols full, 1 with missing bars",
    ]
    assert lines[-1] == (
        "  ticker changes in the window: renames not recorded (no data_ticker_changes table); "
        "exclusions not recorded (no data_universe_exclusions table); KO bars stop after 2026-10-05"
    )


def test_renames_and_exclusions_recorded_at_import_are_ticker_changes(tmp_path):
    path = tmp_path / "m.sqlite3"
    seed(path, stops_after=("KO", date(2026, 10, 5)))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(TICKER_CHANGES_SCHEMA)
        connection.executemany(
            "INSERT INTO data_ticker_changes VALUES (?, ?, ?, ?)",
            [("alpaca-bars-v2", "SKYD", "PSKY", "2026-10-06"),
             ("alpaca-bars-v2", "FI", "FISV", "2025-11-11"),  # before the window
             ("alpaca-bars-v1", "NEWT", "OLDT", "2026-10-01")],  # another bars version
        )  # fmt: skip
        connection.executescript(EXCLUSIONS_SCHEMA)
        connection.executemany(
            "INSERT INTO data_universe_exclusions VALUES (?, ?, ?, ?)",
            [("demo-bundle-v2", "WBD", "no bar on 2026-10-08", "2026-10-07"),
             ("demo-bundle-v2", "ZZZ", "no alpaca-bars-v2 bars", None),
             ("demo-bundle-v1", "OLD", "elsewhere", None)],
        )  # fmt: skip
    lines, _ = report(path, "demo-bundle-v2")
    assert lines[-1] == (
        "  ticker changes in the window: PSKY <- SKYD from 2026-10-06 (recorded at import); "
        "WBD excluded from the universe: no bar on 2026-10-08, last bar 2026-10-07; "
        "ZZZ excluded from the universe: no alpaca-bars-v2 bars; KO bars stop after 2026-10-05"
    )


@pytest.mark.parametrize(
    "schemas, expected",
    [
        ((TICKER_CHANGES_SCHEMA, EXCLUSIONS_SCHEMA), "none"),
        ((TICKER_CHANGES_SCHEMA,), "exclusions not recorded (no data_universe_exclusions table)"),
        ((EXCLUSIONS_SCHEMA,), "renames not recorded (no data_ticker_changes table)"),
    ],
)
def test_empty_change_tables_are_none_and_a_missing_one_is_not_recorded(
    tmp_path, schemas, expected
):
    path = tmp_path / "m.sqlite3"
    seed(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        for schema in schemas:
            connection.executescript(schema)
    lines, failures = report(path, "demo-bundle-v2")
    assert failures == []
    assert lines[-1] == f"  ticker changes in the window: {expected}"


def test_no_stored_universe_fails(tmp_path):
    path = tmp_path / "m.sqlite3"
    db.initialize(path, bundles.SCHEMA)
    with db.write_transaction(path) as connection:
        bundles.seed(connection)
    assert report(path, "demo-bundle-v2")[1] == ["demo-bundle-v2 has no stored universe"]


def test_news_never_imported_fails_unless_allowed(tmp_path, capsys):
    path = tmp_path / "m.sqlite3"
    seed(path, news_coverage="none")
    assert report(path, "demo-bundle-v2")[1] == ["news alpaca-news-v2 was never imported"]
    assert main(["--db", str(path), "--bundle", "demo-bundle-v2", "--allow-missing-news"]) == 0
    assert "WARNING: --allow-missing-news" in capsys.readouterr().out


def test_news_coverage_that_stops_short_is_reported_not_failed(tmp_path):
    seed(tmp_path / "m.sqlite3", news_coverage="short")
    lines, failures = report(tmp_path / "m.sqlite3", "demo-bundle-v2")
    assert failures == []
    aapl = next(line for line in lines if line.startswith("AAPL"))
    assert "news 1 (coverage 2026-09-01..2026-10-05) NEWS DOES NOT COVER THE WINDOW" in aapl
    assert "  news coverage spanning the window: 0/4" in lines


def test_filings_never_imported_fails_unless_allowed(tmp_path):
    path = tmp_path / "m.sqlite3"
    seed(path, filing_symbols=())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE data_filings")
    assert report(path, "demo-bundle-v2")[1] == ["filings edgar-filings-v2 were never imported"]
    assert report(path, "demo-bundle-v2", allow_missing_filings=True)[1] == []


def test_a_symbol_without_a_fiscal_period_end_is_reported_not_failed(tmp_path):
    seed(tmp_path / "m.sqlite3", filing_symbols=("AAPL", "MSFT", "SPY"))
    lines, failures = report(tmp_path / "m.sqlite3", "demo-bundle-v2")
    assert failures == []
    assert any(line.startswith("KO") and "NO FISCAL PERIOD END VISIBLE" in line for line in lines)
    assert "  filings with a fiscal period end by the window end: 3/4" in lines


def test_the_report_opens_paths_with_spaces_and_hashes(tmp_path):
    folder = tmp_path / "market data #2"
    folder.mkdir()
    seed(folder / "m.sqlite3")
    assert main(["--db", str(folder / "m.sqlite3"), "--bundle", "demo-bundle-v2"]) == 0


@pytest.fixture
def gapped(tmp_path):
    seed(tmp_path / "m.sqlite3", missing_days=(WHOLE_DAY,), missing_bar=("AAPL", PARTIAL_DAY))
    with TestClient(create_app(tmp_path / "m.sqlite3")) as client:
        yield client


def test_the_route_lists_whole_and_partial_gaps(gapped):
    body = gapped.get("/bundles/demo-bundle-v2").json()
    assert body["bundle_id"] == "demo-bundle-v2"
    assert (body["bars"], body["news"], body["filings"]) == (
        "alpaca-bars-v2", "alpaca-news-v2", "edgar-filings-v2",
    )  # fmt: skip
    assert (body["start_date"], body["end_date"], body["expected_sessions"]) == (
        "2026-09-28", "2026-10-09", 10,
    )  # fmt: skip
    assert "symbols" not in body
    assert (body["universe_name"], body["benchmark"]) == ("S&P 500", "SPY")
    assert body["universe"] == [
        {"symbol": m.symbol, "name": m.name, "sector": m.sector} for m in UNIVERSE[:3]
    ]
    assert body["sessions"] == [
        d.isoformat() for d in EXPECTED if d not in (WHOLE_DAY, PARTIAL_DAY)
    ]
    assert body["incomplete_sessions"] == [
        {"date": "2026-10-02", "missing": list(SYMBOLS)},
        {"date": "2026-10-06", "missing": ["AAPL"]},
    ]
    assert len(body["sessions"]) + len(body["incomplete_sessions"]) == body["expected_sessions"]


def test_v1_needs_no_credentials_and_reports_every_session_missing(gapped):
    body = gapped.get("/bundles/demo-bundle-v1", headers={"X-Anything": "ignored"}).json()
    assert (body["start_date"], body["end_date"], body["expected_sessions"]) == (
        "2026-02-02", "2026-02-13", 10,
    )  # fmt: skip
    assert [m["symbol"] for m in body["universe"]] == ["AAPL", "KO", "MSFT"]
    assert (body["universe_name"], body["benchmark"], body["sessions"]) == (None, None, [])
    assert len(body["incomplete_sessions"]) == 10  # no v1 bars in this database


def test_an_unknown_bundle_is_404(gapped):
    response = gapped.get("/bundles/demo-bundle-v9")
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")


def test_windows_are_immutable_and_seed_over_an_existing_v1(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(  # seeded before windows existed: v1 components only
            "CREATE TABLE data_bundles (bundle_id TEXT PRIMARY KEY, bars TEXT NOT NULL, "
            "news TEXT NOT NULL, filings TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO data_bundles VALUES "
            "('demo-bundle-v1', 'alpaca-bars-v1', 'alpaca-news-v1', 'edgar-filings-v1')"
        )
    db.initialize(path, bundles.SCHEMA)
    for _ in range(2):
        with db.write_transaction(path) as connection:
            bundles.seed(connection)
    with db.read_connection(path) as connection:
        assert bundles.component(connection, "demo-bundle-v2", "bars") == "alpaca-bars-v2"
        assert bundles.window(connection, "demo-bundle-v2") == V2
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE data_bundle_windows SET holidays = '2026-10-05'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM data_bundle_windows")


def test_a_changed_window_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "m.sqlite3"
    db.initialize(path, bundles.SCHEMA)
    with db.write_transaction(path) as connection:
        bundles.seed(connection)
    changed = {
        **bundles.DEMO_WINDOWS,
        "demo-bundle-v2": bundles.Window(
            V2.start, V2.end, (date(2026, 10, 5),), V2.universe_name, V2.benchmark
        ),
    }
    monkeypatch.setattr(bundles, "DEMO_WINDOWS", changed)
    with pytest.raises(bundles.BundleConflict), db.write_transaction(path) as connection:
        bundles.seed(connection)


def test_a_windows_table_from_before_universes_is_refused(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE data_bundle_windows (bundle_id TEXT PRIMARY KEY, start_date TEXT, "
            "end_date TEXT, universe TEXT, holidays TEXT)"
        )
    db.initialize(path, bundles.SCHEMA)
    with (
        pytest.raises(bundles.BundleConflict, match="predates universes"),
        db.write_transaction(path) as connection,
    ):
        bundles.seed(connection)


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "m.sqlite3"
    db.initialize(path, bundles.SCHEMA)
    with db.write_transaction(path) as connection:
        bundles.seed(connection)
    return path


def test_a_universe_is_stored_once_and_is_immutable(seeded):
    for _ in range(2):
        with db.write_transaction(seeded) as connection:
            bundles.store_universe(connection, "demo-bundle-v2", UNIVERSE)
    with db.read_connection(seeded) as connection:
        assert bundles.universe(connection, "demo-bundle-v2") == UNIVERSE
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE data_universe SET sector = 'Energy'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM data_universe")


@pytest.mark.parametrize(
    "members",
    [
        UNIVERSE[:3]
        + (bundles.Member("SPY", "S&P", "Benchmark", None, benchmark=True),),  # changed
        UNIVERSE[1:],  # a member dropped
        UNIVERSE + (bundles.Member("XOM", "Exxon Mobil", "Energy"),),  # a member added
    ],
)
def test_a_changed_universe_is_refused(seeded, members):
    with db.write_transaction(seeded) as connection:
        bundles.store_universe(connection, "demo-bundle-v2", UNIVERSE)
    with (
        pytest.raises(bundles.BundleConflict, match="different universe"),
        db.write_transaction(seeded) as connection,
    ):
        bundles.store_universe(connection, "demo-bundle-v2", members)


@pytest.mark.parametrize(
    "bundle_id, members",
    [
        ("demo-bundle-v2", UNIVERSE[:3]),  # no benchmark
        ("demo-bundle-v2", UNIVERSE[:3] + (bundles.Member("QQQ", "QQQ", "Benchmark", None, True),)),
        ("demo-bundle-v2", (UNIVERSE[3],)),  # a benchmark and no constituents
        ("demo-bundle-v2", UNIVERSE + (UNIVERSE[0],)),  # a duplicate
        ("demo-bundle-v2", UNIVERSE + (bundles.Member("brk.b", "Berkshire", "Financials"),)),
        ("demo-bundle-v2", UNIVERSE + (bundles.Member("XOM", "Exxon", ""),)),  # no sector
        ("demo-bundle-v2", UNIVERSE + (bundles.Member("XOM", "Exxon", "Benchmark"),)),
        ("demo-bundle-v9", UNIVERSE),  # no window
    ],
)
def test_an_invalid_universe_is_refused_and_writes_nothing(seeded, bundle_id, members):
    with pytest.raises(bundles.BundleConflict), db.write_transaction(seeded) as connection:
        bundles.store_universe(connection, bundle_id, members)
    with db.read_connection(seeded) as connection:
        assert bundles.universe(connection, "demo-bundle-v2") == ()


def test_dotted_share_classes_are_valid_symbols(seeded):
    members = UNIVERSE + (bundles.Member("BRK.B", "Berkshire Hathaway", "Financials", "1067983"),)
    with db.write_transaction(seeded) as connection:
        bundles.store_universe(connection, "demo-bundle-v2", members)
    with db.read_connection(seeded) as connection:
        assert "BRK.B" in [m.symbol for m in bundles.universe(connection, "demo-bundle-v2")]

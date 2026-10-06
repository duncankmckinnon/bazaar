import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from bazaar_market.prices import EASTERN, SqliteMarketData, close_at
from bazaar_market.sources.alpaca_bars import TickerWindow, fetch_bars, ticker_windows
from bazaar_market.sources.bars_import import import_bars_snapshot
from bazaar_market.sources.cli import main
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.snapshot import Snapshot

WEEK = [date(2026, 2, 2) + timedelta(days=n) for n in range(5)]


def bar_json(day, close="101.5"):
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=EASTERN)
    t = midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"t": t, "o": 100, "h": 102.25, "l": 99.5, "c": float(close), "v": 1234, "n": 9}


def payload(ticker, days, token=None):
    return {"bars": {ticker: [bar_json(d) for d in days]}, "next_page_token": token}


def serve(pages, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        ticker = request.url.params["symbols"]
        return httpx.Response(200, content=json.dumps(pages[ticker].pop(0)))

    return httpx.Client(transport=httpx.MockTransport(handler))


def freeze(tmp_path, pages, *, start=WEEK[0], end=WEEK[-1], seen=None):
    snap = Snapshot(tmp_path / "raw", source="alpaca-bars", version="v1")
    client = serve(pages, [] if seen is None else seen)
    for ticker in pages.copy():
        fetch_bars(
            client,
            window=TickerWindow(ticker, start, end),
            snap=snap,
            feed="sip",
            sleep=lambda _: None,
        )
    return snap


def test_a_rename_inside_the_period_splits_the_requests_by_ticker():
    windows = ticker_windows(("AAPL", "FISV"), date(2025, 7, 1), date(2026, 9, 30))

    assert windows == [
        TickerWindow("AAPL", date(2025, 7, 1), date(2026, 9, 30)),
        TickerWindow("FI", date(2025, 7, 1), date(2025, 11, 10)),
        TickerWindow("FISV", date(2025, 11, 11), date(2026, 9, 30)),
    ]


def test_bars_are_requested_unadjusted_daily_and_without_ticker_mapping(tmp_path):
    seen = []
    snap = freeze(tmp_path, {"AAPL": [payload("AAPL", WEEK)]}, seen=seen)

    params = seen[0].url.params
    assert (params["adjustment"], params["timeframe"], params["asof"], params["feed"]) == (
        "raw",
        "1Day",
        "-",
        "sip",
    )
    assert (params["start"], params["end"]) == ("2026-02-02T05:00:00Z", "2026-02-07T04:59:59Z")
    assert snap.coverage("AAPL") == {
        "start": "2026-02-02T05:00:00Z",
        "end": "2026-02-07T04:59:59Z",
        "adjustment": "raw",
        "feed": "sip",
    }


def test_bars_follow_the_page_token_and_keep_exact_prices(tmp_path):
    seen = []
    pages = {"AAPL": [payload("AAPL", WEEK[:3], token="next"), payload("AAPL", WEEK[3:])]}
    snap = Snapshot(tmp_path, source="alpaca-bars", version="v1")

    bars = fetch_bars(
        serve(pages, seen),
        window=TickerWindow("AAPL", WEEK[0], WEEK[-1]),
        snap=snap,
        feed="sip",
        sleep=lambda _: None,
    )

    assert [r.url.params.get("page_token") for r in seen] == [None, "next"]
    assert [b.session for b in bars] == WEEK
    assert (bars[0].open, bars[0].high, bars[0].close) == (
        Decimal(100),
        Decimal("102.25"),
        Decimal("101.5"),
    )


def test_an_empty_result_is_frozen_and_the_import_reports_missing_coverage(tmp_path):
    snap = freeze(tmp_path, {"AAPL": [payload("AAPL", WEEK)], "KO": [{"bars": {}}]})

    assert snap.entry("KO/page-0001.json")["rows"] == 0
    with (
        closing(sqlite3.connect(tmp_path / "m.db")) as connection,
        pytest.raises(SourceError, match="missing coverage.*KO"),
    ):
        import_bars_snapshot(connection, snap.dir, required={})


def test_a_session_one_ticker_lacks_between_its_first_and_last_bar_fails(tmp_path):
    gappy = [WEEK[0], WEEK[1], WEEK[3], WEEK[4]]
    snap = freeze(tmp_path, {"AAPL": [payload("AAPL", WEEK)], "KO": [payload("KO", gappy)]})

    with (
        closing(sqlite3.connect(tmp_path / "m.db")) as connection,
        pytest.raises(SourceError, match="KO has no bar on 2026-02-04"),
    ):
        import_bars_snapshot(connection, snap.dir, required={})


def test_the_runner_demo_window_is_required_by_default(tmp_path):
    snap = freeze(tmp_path, {"AAPL": [payload("AAPL", WEEK)]})

    with (
        closing(sqlite3.connect(tmp_path / "m.db")) as connection,
        pytest.raises(SourceError, match="MSFT is required on 2026-01-30"),
    ):
        import_bars_snapshot(connection, snap.dir)


def test_importing_a_snapshot_twice_stores_each_bar_once(tmp_path):
    snap = freeze(tmp_path, {"AAPL": [payload("AAPL", WEEK)], "KO": [payload("KO", WEEK)]})
    db = tmp_path / "m.db"

    for _ in range(2):
        with closing(sqlite3.connect(db)) as connection:
            report = import_bars_snapshot(connection, snap.dir, required={"KO": set(WEEK)})

    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM data_bars").fetchone()[0] == 10
    assert [(c.ticker, c.bars, c.first, c.last) for c in report] == [
        ("AAPL", 5, WEEK[0], WEEK[-1]),
        ("KO", 5, WEEK[0], WEEK[-1]),
    ]
    market = SqliteMarketData(db, "alpaca-bars-v1")
    assert market.price_at("KO", close_at(WEEK[2])).observed_at == close_at(WEEK[2])
    assert market.price_source == "alpaca/sip/raw/v1"


def test_cli_bars_then_import_bars_end_to_end(tmp_path, capsys):
    config = tmp_path / "sources.toml"
    config.write_text(
        '[period]\nstart = 2026-02-02\nend = 2026-02-06\n[sp500]\ncommit = "abc"\n'
        '[edgar]\nforms = ["10-K"]\ndocuments_since = 2026-01-01\n'
        '[[company]]\nticker = "KO"\ncik = 21344\n'
    )
    seen = []

    main(
        ["bars", "--config", str(config), "--root", str(tmp_path / "raw"), "--version", "v1"],
        env={"ALPACA_API_KEY": "id", "ALPACA_SECRET_KEY": "secret"},
        http=serve({"KO": [payload("KO", WEEK)]}, seen),
    )
    with pytest.raises(SourceError, match="AAPL is required"):
        main(
            [
                "import-bars",
                "--snapshot",
                str(tmp_path / "raw" / "alpaca-bars" / "v1"),
                "--db",
                str(tmp_path / "m.db"),
            ]
        )

    assert {r.url.host for r in seen} == {"data.alpaca.markets"}
    assert seen[0].headers["apca-api-key-id"] == "id"
    assert "bars: KO 5 daily bars" in capsys.readouterr().out

import dataclasses
import json
import sqlite3
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import bazaar_web.tickers
import httpx
import pytest
from bazaar_web.app import create_app
from bazaar_web.bundle import RETRY_S, SYMBOLS, BundleSource, parse
from bazaar_web.store import Store
from bazaar_web.tickers import TickerSource
from fastapi.testclient import TestClient

# The market's bar table (bazaar_market.prices on main), copied so the test builds its own
# synthetic database without importing market code. The web reads only closes from it.
MARKET_SCHEMA = """
CREATE TABLE data_bars (
    data_version TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
    close TEXT NOT NULL, volume INTEGER NOT NULL CHECK (volume >= 0), source TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol, observed_at)
);
"""
FEB = [date(2026, 2, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
SEP_OCT = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)] + [
    date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8, 9)
]
# Synthetic bars: the session before each window, both windows, and a session after each.
BAR_DAYS = (
    [date(2026, 1, 30), *FEB, date(2026, 2, 17)]
    + [date(2026, 9, 25), *SEP_OCT, date(2026, 10, 12)]
)  # fmt: skip


def bundle_body(sessions, *, bundle_id="demo-bundle-v1", bars="synthetic-bars-v1", symbols=None):
    """The shape of the market's GET /bundles/{id}; symbols only when the bundle has them."""
    body = {
        "bundle_id": bundle_id,
        "bars": bars,
        "news": "news-v1",
        "filings": "filings-v1",
        "start_date": sessions[0].isoformat(),
        "end_date": sessions[-1].isoformat(),
        "sessions": [s.isoformat() for s in sessions],
        "incomplete_sessions": [],
    }
    if symbols is not None:
        body["symbols"] = symbols
    return body


V1 = bundle_body(FEB)
V2 = bundle_body(SEP_OCT, bundle_id="demo-bundle-v2", bars="synthetic-bars-v2")


def market(*responses):
    """A MockTransport answering /bundles/{id} with each response in turn (the last repeats)."""
    calls = []

    def handler(request):
        calls.append(request.url.raw_path.decode())
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, json={"detail": "unknown bundle"})
        return httpx.Response(200, json=item)

    return httpx.MockTransport(handler), calls


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def source(*responses, bundle_id="demo-bundle-v1"):
    transport, calls = market(*responses)
    clock = Clock()
    return (
        BundleSource("http://market:8000", bundle_id, transport=transport, clock=clock),
        calls,
        clock,
    )


def stored_close(day: date) -> str:
    close = datetime.combine(day, time(16), ZoneInfo("America/New_York"))
    return close.astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds")


def market_db(path, symbols=SYMBOLS):
    """Symbol k closes at 100 + k + d on bar day d, under both synthetic bars versions."""
    with sqlite3.connect(path) as conn:
        conn.executescript(MARKET_SCHEMA)
        for k, symbol in enumerate(symbols):
            for d, day in enumerate(BAR_DAYS):
                close = str(100 + k + d)
                observed = stored_close(day)
                for version in ("synthetic-bars-v1", "synthetic-bars-v2", "other-bars-v9"):
                    conn.execute(
                        "INSERT INTO data_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1000, 'synthetic')",
                        (version, symbol, observed, observed, close, close, close, close),
                    )
    conn.close()
    return path


# ---------- the bundle route ----------


def test_the_bundle_is_fetched_once_and_kept(tmp_path):
    bundles, calls, _ = source(V1)

    first = bundles.get()
    assert (first.start, first.end, first.bars) == (FEB[0], FEB[-1], "synthetic-bars-v1")
    assert first.sessions == tuple(FEB) and first.symbols == tuple(SYMBOLS)
    assert bundles.get() is first
    assert calls == ["/bundles/demo-bundle-v1"]


@pytest.mark.parametrize(
    "failure", [404, 500, httpx.ConnectError("refused"), {"bundle_id": "x"}, ["not", "a", "dict"]]
)
def test_a_failure_gives_no_bundle_and_retries_at_most_every_30s(failure):
    bundles, calls, clock = source(failure, V2, bundle_id="demo-bundle-v2")

    assert bundles.get() is None
    clock.t += RETRY_S - 1
    assert [bundles.get() for _ in range(5)] == [None] * 5  # polls inside the backoff: no call
    assert len(calls) == 1
    clock.t += 1
    assert bundles.get().start == SEP_OCT[0]  # the market recovered
    assert len(calls) == 2


def test_parse_takes_the_universe_then_symbols_then_the_12():
    assert parse(V1).symbols == tuple(SYMBOLS)
    eleven = [s for s in SYMBOLS if s != "EA"]
    assert parse(bundle_body(SEP_OCT, symbols=eleven)).symbols == tuple(eleven)
    universe = [{"symbol": s, "name": f"{s} Inc.", "sector": "Industrials"} for s in eleven]
    v2 = bundle_body(SEP_OCT) | {
        "universe": universe,
        "universe_name": "S&P 500",
        "benchmark": "SPY",
    }
    assert parse(v2).symbols == tuple(eleven)  # SPY, the benchmark, is not in the universe
    both = v2 | {"symbols": ["AAPL"]}
    assert parse(both).symbols == tuple(eleven)  # universe wins over symbols
    v1_nulls = bundle_body(FEB) | {"universe": None, "universe_name": None, "benchmark": None}
    assert parse(v1_nulls).symbols == tuple(SYMBOLS)
    for bad in ([], [""], [1, 2]):
        with pytest.raises(ValueError):
            parse(bundle_body(SEP_OCT, symbols=bad))
    for bad in ([], [{"symbol": ""}], [{"name": "no symbol"}], ["AAPL"]):
        with pytest.raises((ValueError, KeyError, TypeError)):
            parse(bundle_body(SEP_OCT) | {"universe": bad})
    with pytest.raises(ValueError):
        parse(bundle_body(SEP_OCT) | {"sessions": ["2026-01-05"]})  # outside the window


def test_the_bundle_id_is_quoted_into_the_path():
    bundles, calls, _ = source(404, bundle_id="../admin?x=1")
    bundles.get()
    assert calls == ["/bundles/..%2Fadmin%3Fx%3D1"]


# ---------- the ticker days ----------


@pytest.mark.parametrize(("body", "sessions", "first_k_d"), [(V1, FEB, 1), (V2, SEP_OCT, 13)])
def test_tickers_replay_the_bundle_sessions_only(tmp_path, body, sessions, first_k_d):
    db = market_db(tmp_path / "market.sqlite3")
    payload = TickerSource(db).payload(parse(body))

    days = payload["days"]
    assert payload["data_version"] == body["bundle_id"]
    assert payload["window"] == {
        "start": body["start_date"],
        "end": body["end_date"],
        "symbols": SYMBOLS if isinstance(SYMBOLS, list) else list(SYMBOLS),
    }
    assert [d["date"] for d in days] == [s.isoformat() for s in sessions]  # no bar outside
    assert all([q["symbol"] for q in d["quotes"]] == list(SYMBOLS) for d in days)
    # AAPL (k = 0): the first session's change is from the bar before the window.
    aapl = days[0]["quotes"][0]
    d = first_k_d
    assert (aapl["close"], aapl["change_pct"]) == (100.0 + d, round((1 / (99 + d)) * 100, 2))
    assert all(isinstance(q["close"], float) for d in days for q in d["quotes"])


def test_the_symbols_field_changes_the_ticker_symbols(tmp_path):
    db = market_db(tmp_path / "market.sqlite3")
    eleven = [s for s in SYMBOLS if s != "EA"]

    payload = TickerSource(db).payload(parse(bundle_body(SEP_OCT, symbols=eleven)))

    assert payload["window"]["symbols"] == eleven
    assert all([q["symbol"] for q in d["quotes"]] == eleven for d in payload["days"])


def test_day_one_without_a_previous_bar_has_no_change(tmp_path):
    db = market_db(tmp_path / "m.sqlite3")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM data_bars WHERE observed_at < ?", (stored_close(FEB[0]),))
    conn.close()

    days = TickerSource(db).payload(parse(V1))["days"]

    assert all(q["change_pct"] is None for q in days[0]["quotes"])
    assert days[1]["quotes"][0]["change_pct"] == 0.99


def test_no_bundle_means_no_window_and_no_days(tmp_path):
    payload = TickerSource(market_db(tmp_path / "m.sqlite3")).payload(None)

    assert payload == {
        "window": {"start": None, "end": None, "symbols": list(SYMBOLS)},
        "data_version": None,
        "days": [],
    }


@pytest.mark.parametrize("case", ["unset", "missing", "garbage", "empty"])
def test_no_usable_market_db_gives_no_days(tmp_path, case):
    path = {
        "unset": None,
        "missing": tmp_path / "nope.sqlite3",
        "garbage": tmp_path / "garbage.sqlite3",
        "empty": tmp_path / "empty.sqlite3",
    }[case]
    if case == "garbage":
        path.write_bytes(b"not a database at all")
    if case == "empty":
        sqlite3.connect(path).close()

    payload = TickerSource(path).payload(parse(V1))

    assert (payload["days"], payload["data_version"]) == ([], None)


def test_the_first_good_read_is_cached(tmp_path):
    db = market_db(tmp_path / "m.sqlite3")
    tickers = TickerSource(db)
    first = tickers.payload(parse(V1))
    db.unlink()

    assert tickers.payload(parse(V1)) == first


def test_the_market_db_is_opened_read_only(tmp_path, monkeypatch):
    db = market_db(tmp_path / "m.sqlite3")
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        opened.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(bazaar_web.tickers.sqlite3, "connect", spy)
    TickerSource(db).payload(parse(V1))

    [(args, kwargs)] = opened
    assert args[0].endswith("?mode=ro") and kwargs == {"uri": True, "timeout": 30}
    with (
        real_connect(*args, **kwargs) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute("DELETE FROM data_bars")


@pytest.mark.parametrize("case", ["garbage", "empty"])
def test_a_failing_read_still_closes_its_connection(tmp_path, monkeypatch, case):
    path = tmp_path / "m.sqlite3"
    if case == "garbage":
        path.write_bytes(b"not a database at all")
    else:
        sqlite3.connect(path).close()  # no tables: the query fails
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        opened.append(conn := real_connect(*args, **kwargs))
        return conn

    monkeypatch.setattr(bazaar_web.tickers.sqlite3, "connect", spy)
    tickers = TickerSource(path)
    for _ in range(3):  # each failed poll opens one connection and closes it
        assert tickers.payload(parse(V1))["days"] == []

    assert len(opened) == 3
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


# ---------- through the app ----------


def app_with(settings, helpers, *responses, **changes):
    bundles, calls, clock = source(*responses)
    app = create_app(
        dataclasses.replace(settings, **changes), helpers.FakeRunner(), bundle_source=bundles
    )
    return app, calls, clock


@pytest.mark.parametrize(("body", "start", "end"), [(V1, "2026-02-02", "2026-02-13"),
                                                    (V2, "2026-09-28", "2026-10-09")])  # fmt: skip
def test_the_board_window_comes_from_the_bundle(settings, helpers, tmp_path, body, start, end):
    db = market_db(tmp_path / "m.sqlite3")
    app, calls, _ = app_with(settings, helpers, body, market_db=db)
    with TestClient(app) as c:
        window = c.get("/api/board").json()["window"]
        tickers = c.get("/api/tickers").json()
        for _ in range(3):
            c.get("/api/board")

    assert window == {
        "start": start, "end": end, "starting_cash": 10000.0, "symbols": list(SYMBOLS), "days": 10,
    }  # fmt: skip
    assert tickers["window"]["start"] == start and len(tickers["days"]) == 10
    assert len(calls) == 1  # one market call for every poll


@pytest.mark.parametrize("failure", [404, httpx.ConnectError("refused")])
def test_without_a_bundle_the_board_has_no_window_and_no_ticker_days(
    settings, helpers, tmp_path, failure
):
    app, calls, _ = app_with(settings, helpers, failure, market_db=market_db(tmp_path / "m.db"))
    with TestClient(app) as c:
        board = c.get("/api/board")
        tickers = c.get("/api/tickers")
        c.get("/api/board")

    assert board.status_code == tickers.status_code == 200
    assert {k: board.json()["window"][k] for k in ("start", "end", "days")} == {
        "start": None, "end": None, "days": None,
    }  # fmt: skip
    assert tickers.json()["days"] == [] and tickers.json()["window"]["start"] is None
    assert len(calls) == 1  # retried no sooner than RETRY_S


@pytest.mark.parametrize("shape", ["universe", "symbols"])
def test_the_bundle_symbols_reach_the_board_and_ticker(settings, helpers, tmp_path, shape):
    eleven = [s for s in SYMBOLS if s != "EA"]
    if shape == "universe":
        universe = [{"symbol": s, "name": s, "sector": "Energy"} for s in eleven]
        body = bundle_body(SEP_OCT) | {"universe": universe, "universe_name": "S&P 500"}
    else:
        body = bundle_body(SEP_OCT, symbols=eleven)
    app, _, _ = app_with(settings, helpers, body, market_db=market_db(tmp_path / "m.db"))
    with TestClient(app) as c:
        assert c.get("/api/board").json()["window"]["symbols"] == eleven
        days = c.get("/api/tickers").json()["days"]
    assert days and all([q["symbol"] for q in d["quotes"]] == eleven for d in days)


def test_tickers_and_the_bundle_fetch_emit_no_spans(settings, helpers, tmp_path, capfire):
    app, calls, _ = app_with(settings, helpers, V1, market_db=market_db(tmp_path / "m.db"))
    with TestClient(app) as c:
        capfire.exporter.clear()
        first = c.get("/api/tickers")
        c.get("/api/tickers?x=1")
        c.get("/api/board")

    assert first.status_code == 200 and len(first.json()["days"]) == 10
    assert calls == ["/bundles/demo-bundle-v1"]
    assert capfire.exporter.exported_spans_as_dict() == []


def test_running_history_comes_from_progress_rows(seeded, helpers):
    runner = helpers.FakeRunner(values={"live-bot": "10060.00"})
    runner.gate.clear()
    with TestClient(create_app(seeded, runner)) as c:
        body = {"name": "live-bot", "handle": None, "instructions": "Buy KO on dips, hold MSFT."}
        sid = c.post("/api/submissions", json=body).json()["id"]
        helpers.wait_for(lambda: c.get(f"/api/submissions/{sid}").json()["day"] == 10)
        rows = c.get("/api/board").json()["rows"]
        runner.gate.set()

    live = next(r for r in rows if r["id"] == sid)
    assert live["status"] == "running"
    assert live["history"] == [{"day": d, "value": 10060.0} for d in range(1, 11)]
    queued_or_failed = [r for r in rows if r["status"] in ("queued", "failed")]
    assert all(r["history"] is None for r in queued_or_failed)
    assert json.loads(json.dumps(live["history"])) == live["history"]


def test_progress_table_is_created_on_an_old_db_and_cleared_on_requeue(tmp_path):
    path = tmp_path / "web.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE submissions (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "handle TEXT, instructions TEXT NOT NULL, ip_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, day INTEGER, error TEXT, run_dir TEXT, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "hidden INTEGER NOT NULL DEFAULT 0, latest_value TEXT, trace_context TEXT);"
            "CREATE TABLE events (t TEXT NOT NULL, text TEXT NOT NULL, submission_id TEXT);"
            "INSERT INTO submissions (id, name, instructions, ip_hash, status, created_at) "
            "VALUES ('old1', 'old-bot', 'x', 'h', 'running', '2026-10-08T14:00:00+00:00');"
        )
    conn.close()

    store = Store(path)
    tables = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert "submission_progress" in tables
    store.set_day("old1", 1, Decimal("10010.5"))
    store.set_day("old1", 2, Decimal(10020))
    store.set_day("old1", 3)  # old runner shape: no value, no progress row
    assert store.progress(["old1"]) == {"old1": [(1, "10010.5"), (2, "10020")]}

    store.requeue("old1")

    assert store.progress(["old1"]) == {}

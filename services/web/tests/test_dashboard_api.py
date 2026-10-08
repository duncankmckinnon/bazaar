import dataclasses
import json
import sqlite3
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import bazaar_web.tickers
import pytest
from bazaar_web.app import create_app
from bazaar_web.board import SYMBOLS
from bazaar_web.store import Store
from bazaar_web.tickers import TickerSource
from fastapi.testclient import TestClient

# The market's bar and bundle tables (bazaar_market.prices / bundles on main), copied so the test
# builds its own synthetic database without importing market code.
MARKET_SCHEMA = """
CREATE TABLE data_bars (
    data_version TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
    close TEXT NOT NULL, volume INTEGER NOT NULL CHECK (volume >= 0), source TEXT NOT NULL,
    PRIMARY KEY (data_version, symbol, observed_at)
);
CREATE TABLE data_bundles (
    bundle_id TEXT PRIMARY KEY, bars TEXT NOT NULL, news TEXT NOT NULL, filings TEXT NOT NULL
);
"""
# Jan 30 (the bar before the window), the window's 10 sessions, and Feb 17 (after it).
SESSIONS = [date(2026, 1, 30)] + [date(2026, 2, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
AFTER = date(2026, 2, 17)


def stored_close(day: date) -> str:
    close = datetime.combine(day, time(16), ZoneInfo("America/New_York"))
    return close.astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds")


def market_db(path, *, bars_version="synthetic-bars-v1", with_bundle=True):
    """Symbol k closes at 100 + k + d on session d (d = 0 for Jan 30)."""
    with sqlite3.connect(path) as conn:
        conn.executescript(MARKET_SCHEMA)
        if with_bundle:
            conn.execute(
                "INSERT INTO data_bundles VALUES ('demo-bundle-v1', ?, 'news-v1', 'filings-v1')",
                (bars_version,),
            )
        for k, symbol in enumerate(SYMBOLS):
            for d, day in enumerate([*SESSIONS, AFTER]):
                close = str(100 + k + d)
                observed = stored_close(day)
                for version in (bars_version, "other-bars-v9"):
                    conn.execute(
                        "INSERT INTO data_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1000, 'synthetic')",
                        (version, symbol, observed, observed, close, close, close, close),
                    )
    conn.close()
    return path


def test_tickers_replay_the_window_with_day_over_day_changes(tmp_path):
    payload = TickerSource(market_db(tmp_path / "market.sqlite3"), "demo-bundle-v1").payload()

    days = payload["days"]
    assert payload["data_version"] == "demo-bundle-v1"
    assert payload["window"]["symbols"] == SYMBOLS
    assert [d["date"] for d in days] == [s.isoformat() for s in SESSIONS[1:]]
    assert all([q["symbol"] for q in d["quotes"]] == SYMBOLS for d in days)
    aapl = [d["quotes"][0] for d in days]
    # AAPL: Jan 30 100 -> Feb 2 101 (+1.00%), then 102 (101 -> 102 = +0.99%).
    assert (aapl[0]["close"], aapl[0]["change_pct"]) == (101.0, 1.0)
    assert (aapl[1]["close"], aapl[1]["change_pct"]) == (102.0, 0.99)
    xom = days[-1]["quotes"][-1]  # k = 11: Feb 13 is d = 10, 121 vs 120
    assert (xom["symbol"], xom["close"], xom["change_pct"]) == ("XOM", 121.0, 0.83)
    assert all(isinstance(q["close"], float) for d in days for q in d["quotes"])


def test_a_plain_bars_version_works_without_bundles(tmp_path):
    db = market_db(tmp_path / "m.sqlite3", bars_version="alpaca-bars-v1", with_bundle=False)

    payload = TickerSource(db, "alpaca-bars-v1").payload()

    assert len(payload["days"]) == 10


def test_day_one_without_a_previous_bar_has_no_change(tmp_path):
    db = market_db(tmp_path / "m.sqlite3")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM data_bars WHERE observed_at < ?", (stored_close(SESSIONS[1]),))
    conn.close()

    days = TickerSource(db, "demo-bundle-v1").payload()["days"]

    assert all(q["change_pct"] is None for q in days[0]["quotes"])
    assert days[1]["quotes"][0]["change_pct"] == 0.99


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

    payload = TickerSource(path, "demo-bundle-v1").payload()

    assert (payload["days"], payload["data_version"]) == ([], None)


def test_the_first_good_read_is_cached(tmp_path):
    db = market_db(tmp_path / "m.sqlite3")
    source = TickerSource(db, "demo-bundle-v1")
    first = source.payload()
    db.unlink()

    assert source.payload() == first


def test_the_market_db_is_opened_read_only(tmp_path, monkeypatch):
    db = market_db(tmp_path / "m.sqlite3")
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        opened.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(bazaar_web.tickers.sqlite3, "connect", spy)
    TickerSource(db, "demo-bundle-v1").payload()

    [(args, kwargs)] = opened
    assert args[0].endswith("?mode=ro") and kwargs == {"uri": True}
    with (
        real_connect(*args, **kwargs) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute("DELETE FROM data_bars")


def test_tickers_endpoint_and_no_spans(settings, helpers, tmp_path, capfire):
    db = market_db(tmp_path / "m.sqlite3")
    with TestClient(
        create_app(dataclasses.replace(settings, market_db=db), helpers.FakeRunner())
    ) as c:
        capfire.exporter.clear()
        first = c.get("/api/tickers")
        c.get("/api/tickers?x=1")

    assert first.status_code == 200
    assert len(first.json()["days"]) == 10
    assert capfire.exporter.exported_spans_as_dict() == []


def test_tickers_endpoint_without_a_market_db(settings, helpers):
    with TestClient(create_app(settings, helpers.FakeRunner())) as c:
        response = c.get("/api/tickers")

    assert response.status_code == 200
    assert response.json()["days"] == []


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

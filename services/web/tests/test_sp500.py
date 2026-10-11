"""S4: the S&P 500 board. Universe label from the bundle, top movers on the tape."""

import logging
import sqlite3
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import bazaar_web.tickers
import pytest
from bazaar_web.board import build_board
from bazaar_web.bundle import Bundle, parse
from bazaar_web.settings import Settings
from bazaar_web.tickers import TOP_MOVERS, TickerSource, movers

SESSIONS = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
BEFORE = date(2026, 9, 25)


def close_at(day: date) -> str:
    at = datetime.combine(day, time(16), ZoneInfo("America/New_York"))
    return at.astimezone(ZoneInfo("UTC")).isoformat(timespec="microseconds")


def db_with(path, closes: dict[str, list[float]], bars="sp-bars-v2"):
    """closes[symbol] = [close on BEFORE, then on each session]."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE data_bars (data_version TEXT, symbol TEXT, observed_at TEXT, "
            "available_at TEXT, open TEXT, high TEXT, low TEXT, close TEXT, volume INTEGER, "
            "source TEXT)"
        )
        rows = [
            (bars, symbol, close_at(day), close_at(day), str(c), str(c), str(c), str(c))
            for symbol, values in closes.items()
            for day, c in zip([BEFORE, *SESSIONS], values, strict=True)
        ]
        conn.executemany("INSERT INTO data_bars VALUES (?,?,?,?,?,?,?,?,1,'s')", rows)
    conn.close()
    return path


def bundle(symbols, *, name=None, benchmark=None, bars="sp-bars-v2"):
    body = {
        "bundle_id": "demo-bundle-v2",
        "bars": bars,
        "news": "n",
        "filings": "f",
        "start_date": SESSIONS[0].isoformat(),
        "end_date": SESSIONS[-1].isoformat(),
        "sessions": [s.isoformat() for s in SESSIONS],
        "incomplete_sessions": [],
        "universe": [{"symbol": s, "name": s, "sector": "Industrials"} for s in symbols],
        "universe_name": name,
        "benchmark": benchmark,
    }
    return parse(body)


# ---------- the universe label ----------


def test_the_universe_label_is_the_bundle_name_else_the_count():
    symbols = [f"S{i:03d}" for i in range(503)]
    assert bundle(symbols, name="S&P 500").universe == "S&P 500"
    assert bundle(symbols).universe == "503 STOCKS"
    assert bundle([*symbols, "SPY"], benchmark="SPY").universe == "503 STOCKS"  # not counted
    assert bundle(symbols, name="  ").universe == "503 STOCKS"


def test_the_board_window_carries_the_universe_label(settings):
    from bazaar_web.board import BoardSource
    from bazaar_web.store import Store

    store = Store(settings.web_db)
    payload = build_board(
        BoardSource(settings.runs_dir), store, bundle=bundle(["AAA", "BBB"], name="S&P 500")
    )
    assert (payload["window"]["universe"], payload["window"]["symbols"]) == (
        "S&P 500",
        ["AAA", "BBB"],
    )
    none = build_board(BoardSource(settings.runs_dir), store, bundle=None)["window"]
    assert (none["universe"], none["symbols"]) == (None, None)


def test_bad_universe_name_or_benchmark_is_refused():
    for field in ("universe_name", "benchmark"):
        with pytest.raises(ValueError):
            parse(
                {
                    "bundle_id": "b",
                    "bars": "x",
                    "start_date": "2026-09-28",
                    "end_date": "2026-09-30",
                    "sessions": ["2026-09-28"],
                    field: ["not", "a", "string"],
                }
            )


# ---------- the top movers ----------


def test_a_day_shows_its_top_20_movers_by_size_of_change(tmp_path):
    # 60 symbols; symbol i moves +i% on day one (odd i down instead), so the order is known.
    closes = {}
    for i in range(60):
        move = (i if i % 2 == 0 else -i) / 100
        closes[f"S{i:02d}"] = [100.0, 100 * (1 + move), 100 * (1 + move), 100 * (1 + move)]
    closes["SPY"] = [100.0, 150.0, 150.0, 150.0]  # the biggest move, but a benchmark
    universe = bundle([*closes], benchmark="SPY")

    db = db_with(tmp_path / "m.db", closes)
    with sqlite3.connect(db) as conn:  # no bars at all on the last session
        conn.execute("DELETE FROM data_bars WHERE observed_at = ?", (close_at(SESSIONS[2]),))
    conn.close()

    days = TickerSource(db).payload(universe)["days"]

    first = days[0]["quotes"]
    assert len(first) == TOP_MOVERS == 20
    assert [q["symbol"] for q in first] == [f"S{i:02d}" for i in range(59, 39, -1)]
    assert [q["change_pct"] for q in first[:2]] == [-59.0, 58.0]  # sign kept, ranked by size
    assert "SPY" not in {q["symbol"] for d in days for q in d["quotes"]}
    # Day two has bars (all flat): shown. Day three has none: left out rather than shown empty.
    assert [d["date"] for d in days] == ["2026-09-28", "2026-09-29"]
    assert {q["change_pct"] for q in days[1]["quotes"]} == {0.0}


def test_ties_keep_universe_order():
    quotes = [{"symbol": s, "close": 1.0, "change_pct": c} for s, c in
              [("A", 1.0), ("B", -2.0), ("C", 2.0), ("D", None), ("E", -1.0)]]  # fmt: skip
    assert [q["symbol"] for q in movers(quotes)] == ["B", "C", "A", "E"]


def test_more_than_999_symbols_are_read_in_chunks(tmp_path, monkeypatch):
    closes = {f"T{i:04d}": [100.0, 100.0 + (i % 50), 100.0, 100.0] for i in range(1200)}
    db = db_with(tmp_path / "m.db", closes)
    real_connect = sqlite3.connect
    queries = []

    class Limited:
        """SQLite as built with the historical 999-variable limit."""

        def __init__(self, conn):
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            self.conn = conn

        def execute(self, sql, params=()):
            queries.append(len(params))
            return self.conn.execute(sql, params)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(
        bazaar_web.tickers.sqlite3, "connect", lambda *a, **k: Limited(real_connect(*a, **k))
    )
    days = TickerSource(db).payload(bundle([*closes]))["days"]

    assert [d["date"] for d in days] == [s.isoformat() for s in SESSIONS]
    assert len(days[0]["quotes"]) == TOP_MOVERS
    assert days[0]["quotes"][0]["change_pct"] == 49.0
    assert all(n <= 999 for n in queries) and len(queries) == 2  # 900 + 300 symbols


def test_movers_are_read_once_and_cached(tmp_path, monkeypatch):
    closes = {f"S{i}": [100.0, 101.0 + i, 100.0, 100.0] for i in range(30)}
    source = TickerSource(db_with(tmp_path / "m.db", closes))
    reads = []
    real = bazaar_web.tickers.read_days
    monkeypatch.setattr(bazaar_web.tickers, "read_days", lambda *a: reads.append(1) or real(*a))
    universe = bundle([*closes])

    first = source.payload(universe)
    assert source.payload(universe) == first
    assert len(reads) == 1


# ---------- the one bundle id ----------


def test_the_bundle_id_comes_from_bazaar_data_bundle(monkeypatch, caplog):
    monkeypatch.delenv("BAZAAR_TICKER_DATA_VERSION", raising=False)
    monkeypatch.delenv("BAZAAR_DATA_BUNDLE", raising=False)
    assert Settings.from_env().data_bundle == "demo-bundle-v1"
    monkeypatch.setenv("BAZAAR_DATA_BUNDLE", "demo-bundle-v2")
    monkeypatch.setenv("BAZAAR_TICKER_DATA_VERSION", "old-bundle")
    with caplog.at_level(logging.WARNING, logger="bazaar_web.settings"):
        assert Settings.from_env().data_bundle == "demo-bundle-v2"  # the new name wins
    assert caplog.records == []


def test_the_old_variable_still_works_with_a_warning(monkeypatch, caplog):
    monkeypatch.delenv("BAZAAR_DATA_BUNDLE", raising=False)
    monkeypatch.setenv("BAZAAR_TICKER_DATA_VERSION", "old-bundle")
    with caplog.at_level(logging.WARNING, logger="bazaar_web.settings"):
        assert Settings.from_env().data_bundle == "old-bundle"
    assert [r.getMessage() for r in caplog.records] == [
        "BAZAAR_TICKER_DATA_VERSION is deprecated; set BAZAAR_DATA_BUNDLE instead"
    ]


def test_a_bundle_without_new_fields_keeps_its_symbols():
    v1 = Bundle(
        bundle_id="x", bars="b", start=SESSIONS[0], end=SESSIONS[-1], sessions=(),
        symbols=("AAA",), days=3,
    )  # fmt: skip
    assert (v1.universe_name, v1.benchmark, v1.universe) == (None, None, "1 STOCKS")


# ---------- the reserved handle ----------

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}


def post_handle(settings, helpers, handle, headers=None):
    from bazaar_web.app import create_app
    from bazaar_web.store import OPERATOR_CONTACT_EMAIL
    from fastapi.testclient import TestClient

    # The house scripts send the operator contact (V7 makes contact details required).
    operator = {"email": OPERATOR_CONTACT_EMAIL, "company": "Bazaar operators", "role": "house"}
    body = {
        "name": "some-bot", "handle": handle, "instructions": "Buy KO on dips, hold MSFT.",
        **operator,
    }  # fmt: skip
    with TestClient(create_app(settings, helpers.FakeRunner())) as client:
        return client.post("/api/submissions", json=body, headers=headers or {})


def reserved(response):
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {"type": "value_error", "loc": ["body", "handle"], "msg": "That handle is reserved"}
        ]
    }
    return True


@pytest.mark.parametrize("handle", ["house", "House", "HOUSE", " house ", "\thoUSe\n"])
def test_attendees_cannot_take_the_house_handle(settings, helpers, handle):
    assert reserved(post_handle(settings, helpers, handle))


@pytest.mark.parametrize("token", ["wrong", "", "admin-secret ", "ADMIN-SECRET"])
def test_a_wrong_token_gets_the_same_422(settings, helpers, token):
    assert reserved(post_handle(settings, helpers, "house", {"X-Bazaar-Admin-Token": token}))


def test_with_the_admin_token_the_house_handle_is_allowed(settings, helpers):
    from bazaar_web.store import Store

    response = post_handle(settings, helpers, " House ", ADMIN)

    assert response.status_code == 201, response.text
    assert Store(settings.web_db).get(response.json()["id"])["handle"] == "house"  # normalised


def test_with_no_admin_token_configured_even_a_header_gets_the_422(settings, helpers):
    import dataclasses

    unset = dataclasses.replace(settings, admin_token=None)
    assert reserved(post_handle(unset, helpers, "house", ADMIN))


@pytest.mark.parametrize("handle", ["@alice", "household", "the-house", None, "   "])
def test_other_handles_need_no_token(settings, helpers, handle):
    response = post_handle(settings, helpers, handle)
    assert response.status_code == 201, response.text


def test_the_reserved_422_is_byte_identical_with_and_without_a_wrong_token(settings, helpers):
    bare = post_handle(settings, helpers, "house")
    wrong = post_handle(settings, helpers, "house", {"X-Bazaar-Admin-Token": "wrong"})
    assert (bare.status_code, wrong.status_code) == (422, 422)
    assert bare.content == wrong.content
    assert {k: v for k, v in bare.headers.items() if k != "date"} == {
        k: v for k, v in wrong.headers.items() if k != "date"
    }


def test_each_refusal_raises_a_new_exception():
    from bazaar_web.app import reserved_handle

    first, second = reserved_handle(), reserved_handle()
    assert first is not second and first.detail == second.detail
    assert first.status_code == 422

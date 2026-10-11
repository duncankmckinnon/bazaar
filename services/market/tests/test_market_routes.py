"""GET /experiments/{eid}/market/{screen,quotes,search,sectors} against the real app.

Every expected number is computed by hand from the closes below. Bars after the as-of session
are 999.99 with volume 9,999,999, so any lookahead shows up in the numbers.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from bazaar_market import bundles, db
from bazaar_market.app import create_app
from bazaar_market.prices import close_at, ensure_schema
from fastapi.testclient import TestClient

TOKEN = "runner-secret"
BUNDLE = "demo-bundle-v2"
BARS = "alpaca-bars-v2"
IT, STAPLES, ENERGY = "Information Technology", "Consumer Staples", "Energy"
UNIVERSE = (
    bundles.Member("AAPL", "Apple Inc.", IT),
    bundles.Member("GOOGL", "Alphabet Inc. (Class A)", IT),
    bundles.Member("KO", "Coca-Cola Company (The)", STAPLES),
    bundles.Member("MSFT", "Microsoft", IT),
    bundles.Member("NVDA", "Nvidia", IT),
    bundles.Member("PG", "Procter & Gamble", STAPLES),
    bundles.Member("XOM", "ExxonMobil", ENERGY),
    bundles.Member("SPY", "SPDR S&P 500 ETF Trust", "Benchmark", None, benchmark=True),
)
SYMBOLS = [m.symbol for m in UNIVERSE]

D0921, D0925, D0928 = date(2026, 9, 21), date(2026, 9, 25), date(2026, 9, 28)
D0930, D1001, D1002 = date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)
# as_of 10-02: the previous session is 10-01 and the fifth back is 09-25.
CLOSES = {  # symbol: (09-25, 10-01, 10-02); None is no bar
    "AAPL": ("100.00", "200.00", "210.00"),  # 1d +5.00, 5d +110.00
    "GOOGL": ("100.00", "100.00", "105.00"),  # 1d +5.00 (ties AAPL), 5d +5.00
    "MSFT": ("100.00", "105.00", "105.00"),  # 1d 0.00, 5d +5.00 (ties GOOGL)
    "NVDA": ("50.00", None, "40.00"),  # 1d null, 5d -20.00
    "KO": ("60.00", "60.00", "59.40"),  # 1d -1.00, 5d -1.00
    "PG": ("80.00", "80.00", "81.70"),  # 1d +2.125 -> "2.12" half-even, 5d the same
    "XOM": ("70.00", "70.00", None),  # no bar at the as-of session
    "SPY": ("500.00", "500.00", "505.00"),  # 1d +1.00, 5d +1.00
}
VOLUME = {"AAPL": 3000, "GOOGL": 1000, "MSFT": 3000, "NVDA": 5000, "KO": 1000, "PG": 2000,
          "XOM": 4000, "SPY": 9000}  # fmt: skip
FUTURE = ("999.99", 9_999_999)


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)
            if (start + timedelta(n)).weekday() < 5]  # fmt: skip


def close_on(symbol: str, day: date) -> tuple[str, int] | None:
    base, previous, today = CLOSES[symbol]
    if day > D1002:
        return FUTURE
    if day == D1002:
        return (today, VOLUME[symbol]) if today else None
    if day == D1001:
        return (previous, VOLUME[symbol]) if previous else None
    if day == D0921 and symbol == "AAPL":
        return ("50.00", 1)  # AAPL 5d at as_of 09-28 is 100.00 / 50.00 - 1
    if day < D0921:
        return ("1.00", 1)  # before every lookback this file uses
    return (base, VOLUME[symbol])  # 09-22 .. 09-30


def build(
    path,
    late: dict[tuple[str, date], datetime] | None = None,
    members: tuple[bundles.Member, ...] = UNIVERSE,
) -> None:
    """The bundle seeded with `members`; bars 09-07 .. 10-09. `late` sets available_at."""
    db.initialize(path, bundles.SCHEMA)
    with db.write_transaction(path) as connection:
        bundles.seed(connection)
        bundles.store_universe(connection, BUNDLE, members)
    rows = []
    for symbol in SYMBOLS:
        for day in weekdays(date(2026, 9, 7), date(2026, 10, 9)):
            bar = close_on(symbol, day)
            if bar is None:
                continue
            observed = close_at(day)
            available = (late or {}).get((symbol, day), observed)
            close, volume = bar
            rows.append((BARS, symbol, db.format_time(observed), db.format_time(available),
                         close, close, close, close, volume, "synthetic"))  # fmt: skip
    with closing(sqlite3.connect(path)) as connection, connection:
        ensure_schema(connection)
        connection.executemany("INSERT INTO data_bars VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


class Approvals:
    def __init__(self) -> None:
        self.bound: dict[UUID, UUID] = {}

    def grant(self, experiment_id: UUID) -> UUID:
        approval_id = uuid4()
        self.bound[approval_id] = experiment_id
        return approval_id

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return self.bound.get(approval_id) == experiment_id


class Market:
    def __init__(self, path, data_version: str = BUNDLE) -> None:
        self.approvals = Approvals()
        self.http = TestClient(create_app(path, self.approvals, runner_token=TOKEN))
        self.http.__enter__()
        self.data_version = data_version
        self.experiment_id = uuid4()
        self.approval = self.approvals.grant(self.experiment_id)
        self.started = False

    def at(self, cutoff: datetime) -> "Market":
        body = {"cutoff": cutoff.isoformat()}
        if not self.started:
            body |= {"data_version": self.data_version, "execution_rule_version": "exec-v1"}
            self.started = True
        response = self.http.put(
            f"/experiments/{self.experiment_id}/cutoff",
            json=body,
            headers={"X-Bazaar-Approval": str(self.approval), "X-Bazaar-Runner-Token": TOKEN},
        )
        assert response.status_code == 200, response.text
        return self

    def get(self, route: str, approval: UUID | None = None, **params):
        headers = {"X-Bazaar-Approval": str(approval or self.approval)}
        url = f"/experiments/{self.experiment_id}/market/{route}"
        return self.http.get(url, params=params, headers=headers)

    def ok(self, route: str, **params) -> dict:
        response = self.get(route, **params)
        assert response.status_code == 200, response.text
        return response.json()

    def close(self) -> None:
        self.http.__exit__(None, None, None)


@pytest.fixture
def market(tmp_path):
    build(tmp_path / "m.sqlite3")
    market = Market(tmp_path / "m.sqlite3").at(close_at(D1002))
    yield market
    market.close()


def screen_symbols(market: Market, **params) -> tuple[list[str], int]:
    body = market.ok("screen", **params)
    return [row["symbol"] for row in body["rows"]], body["excluded"]


# --- the cutoff decides the session -------------------------------------------------------


def test_one_microsecond_before_a_close_gives_the_previous_session(tmp_path):
    build(tmp_path / "m.sqlite3")
    market = Market(tmp_path / "m.sqlite3").at(close_at(D1001) - timedelta(microseconds=1))
    body = market.ok("quotes", symbols="AAPL")
    assert body["as_of_session"] == "2026-09-30"
    assert body["cutoff_at"] == "2026-10-01T19:59:59.999999Z"
    assert (body["quotes"][0]["close"], body["quotes"][0]["observed_at"]) == (
        "100.00", "2026-09-30T20:00:00Z",
    )  # fmt: skip
    assert body["quotes"][0]["pct_change_1d"] == "0.00"

    market.at(close_at(D1001))
    body = market.ok("quotes", symbols="AAPL")
    assert body["as_of_session"] == "2026-10-01"
    assert body["quotes"][0]["close"] == "200.00"
    assert body["quotes"][0]["pct_change_1d"] == "100.00"
    market.close()


def test_at_the_first_window_open_the_as_of_session_is_the_last_close_before_it(tmp_path):
    build(tmp_path / "m.sqlite3")
    opening = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)  # 09:30 New York, day 1 of the window
    market = Market(tmp_path / "m.sqlite3").at(opening)
    body = market.ok("quotes", symbols="AAPL,SPY")
    assert (body["as_of_session"], body["cutoff_at"]) == ("2026-09-25", "2026-09-28T13:30:00Z")
    aapl = body["quotes"][0]
    # 09-25 against 09-24 (100.00) and against 09-18 (1.00), five sessions back; 09-28's bar,
    # observed at 20:00Z after the cutoff, is never read.
    assert (aapl["close"], aapl["observed_at"], aapl["pct_change_1d"], aapl["pct_change_5d"]) == (
        "100.00", "2026-09-25T20:00:00Z", "0.00", "9900.00",
    )  # fmt: skip
    assert market.ok("sectors")["as_of_session"] == "2026-09-25"
    assert market.ok("screen")["as_of_session"] == "2026-09-25"
    market.at(close_at(D0928))
    assert market.ok("sectors")["as_of_session"] == "2026-09-28"
    market.close()


def test_the_screen_and_the_fill_engine_agree_on_the_latest_session(tmp_path):
    build(tmp_path / "m.sqlite3")
    market = Market(tmp_path / "m.sqlite3").at(datetime(2026, 9, 28, 13, 30, tzinfo=UTC))
    runner = {"X-Bazaar-Approval": str(market.approval), "X-Bazaar-Runner-Token": TOKEN}
    account = market.http.post(
        f"/experiments/{market.experiment_id}/accounts",
        headers=runner,
        json={"request_id": str(uuid4()), "agent_id": str(uuid4()),
              "strategy_version_id": str(uuid4()), "cash": "1000.00"},
    ).json()["account_id"]  # fmt: skip
    fill = market.http.post(
        f"/experiments/{market.experiment_id}/accounts/{account}/orders",
        headers={"X-Bazaar-Approval": str(market.approval)},
        json={"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "1"},
    ).json()
    quote = market.ok("quotes", symbols="AAPL")["quotes"][0]
    assert fill["status"] == "filled"
    assert (fill["unit_price"], fill["price_observed_at"]) == (
        quote["close"], quote["observed_at"],
    )  # fmt: skip
    market.close()


def test_a_cutoff_with_no_closed_session_in_the_lookback_is_409(tmp_path):
    build(tmp_path / "m.sqlite3")  # the first bars close on 09-07
    market = Market(tmp_path / "m.sqlite3").at(
        close_at(date(2026, 9, 7)) - timedelta(microseconds=1)
    )
    for route, params in (("screen", {}), ("quotes", {"symbols": "AAPL"}), ("sectors", {})):
        response = market.get(route, **params)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "experiment_not_running"
    assert market.ok("search", q="AAPL")["results"][0]["symbol"] == "AAPL"  # static, no prices
    market.at(close_at(date(2026, 9, 7)))
    assert market.ok("sectors")["as_of_session"] == "2026-09-07"
    market.close()


def test_a_bar_available_after_the_cutoff_is_never_used(tmp_path):
    build(tmp_path / "m.sqlite3", late={("KO", D1002): close_at(D1002) + timedelta(hours=1)})
    market = Market(tmp_path / "m.sqlite3").at(close_at(D1002))
    body = market.ok("quotes", symbols="KO,PG")
    assert body["missing"] == [{"symbol": "KO", "reason": "no_bar_at_cutoff"}]
    assert [q["symbol"] for q in body["quotes"]] == ["PG"]
    assert screen_symbols(market, sector=STAPLES) == (["PG"], 1)
    staples = next(s for s in market.ok("sectors")["sectors"] if s["sector"] == STAPLES)
    assert (staples["with_bars"], staples["avg_pct_change_1d"]) == (1, "2.12")

    market.at(close_at(D1002) + timedelta(hours=1))
    body = market.ok("quotes", symbols="KO")
    assert body["as_of_session"] == "2026-10-02" and body["missing"] == []
    assert body["quotes"][0]["available_at"] == "2026-10-02T21:00:00Z"
    market.close()


def test_a_late_previous_bar_makes_the_change_null_and_never_skips_back(tmp_path):
    build(tmp_path / "m.sqlite3", late={("GOOGL", D1001): close_at(date(2026, 10, 5))})
    market = Market(tmp_path / "m.sqlite3").at(close_at(D1002))
    quote = market.ok("quotes", symbols="GOOGL")["quotes"][0]
    # 09-30's close (100.00) would give +5.00; 10-01's bar is not available yet, so null.
    assert (quote["close"], quote["pct_change_1d"], quote["pct_change_5d"]) == (
        "105.00", None, "5.00",
    )  # fmt: skip
    market.close()


def test_a_session_whose_bars_are_all_late_still_counts_as_a_session(tmp_path):
    late = {(symbol, D1001): close_at(date(2026, 10, 5)) for symbol in SYMBOLS}
    build(tmp_path / "m.sqlite3", late=late)
    market = Market(tmp_path / "m.sqlite3").at(close_at(D1002))
    body = market.ok("quotes", symbols="AAPL,KO")
    # 10-01 closed before the cutoff, so it is the previous session even though none of its
    # bars is available: 1d is null, never a change against 09-30; 5d is still against 09-25.
    assert [(q["pct_change_1d"], q["pct_change_5d"]) for q in body["quotes"]] == [
        (None, "110.00"), (None, "-1.00"),
    ]  # fmt: skip
    market.close()


def test_five_day_changes_use_only_sessions_up_to_the_as_of_session(tmp_path):
    build(tmp_path / "m.sqlite3")
    market = Market(tmp_path / "m.sqlite3").at(close_at(D0928))
    aapl = market.ok("quotes", symbols="AAPL")["quotes"][0]
    assert (aapl["close"], aapl["pct_change_1d"], aapl["pct_change_5d"]) == (
        "100.00", "0.00", "100.00",  # against 09-25 and 09-21, never 10-05 .. 10-09
    )  # fmt: skip
    assert aapl["volume"] == 3000
    market.close()


def test_every_value_comes_from_the_as_of_session_or_before(market):
    body = market.ok("quotes", symbols=",".join(SYMBOLS))
    assert body["as_of_session"] == "2026-10-02" and body["data_version"] == BUNDLE
    assert all(q["close"] != FUTURE[0] and q["volume"] != FUTURE[1] for q in body["quotes"])
    assert {q["observed_at"] for q in body["quotes"]} == {"2026-10-02T20:00:00Z"}


# --- screen -------------------------------------------------------------------------------


def test_the_screen_orders_ties_by_symbol_and_counts_null_keys_as_excluded(market):
    assert screen_symbols(market) == (["AAPL", "GOOGL", "PG", "MSFT", "KO"], 2)
    assert screen_symbols(market, direction="asc") == (["KO", "MSFT", "PG", "AAPL", "GOOGL"], 2)
    assert screen_symbols(market, sort="pct_change_5d") == (
        ["AAPL", "GOOGL", "MSFT", "PG", "KO", "NVDA"], 1,
    )  # fmt: skip
    assert screen_symbols(market, sort="volume") == (
        ["NVDA", "AAPL", "MSFT", "PG", "GOOGL", "KO"], 1,
    )  # fmt: skip
    assert screen_symbols(market, sort="volume", direction="asc") == (
        ["GOOGL", "KO", "PG", "AAPL", "MSFT", "NVDA"], 1,
    )  # fmt: skip


def test_the_screen_filters_by_sector_limits_and_never_lists_the_benchmark(market):
    assert screen_symbols(market, sector=IT) == (["AAPL", "GOOGL", "MSFT"], 1)
    assert screen_symbols(market, sector=ENERGY) == ([], 1)
    assert screen_symbols(market, limit="2") == (["AAPL", "GOOGL"], 2)
    assert "SPY" not in screen_symbols(market, sort="volume", limit="50")[0]


def test_a_screen_row_has_the_contract_fields(market):
    body = market.ok("screen", limit="1")
    assert {k: body[k] for k in ("sort", "direction", "sector", "excluded")} == {
        "sort": "pct_change_1d", "direction": "desc", "sector": None, "excluded": 2,
    }  # fmt: skip
    assert body["rows"] == [{
        "symbol": "AAPL", "name": "Apple Inc.", "sector": IT, "close": "210.00",
        "pct_change_1d": "5.00", "pct_change_5d": "110.00", "volume": 3000,
        "observed_at": "2026-10-02T20:00:00Z",
    }]  # fmt: skip
    pg = market.ok("screen", sector=STAPLES)["rows"][0]
    assert (pg["symbol"], pg["pct_change_1d"]) == ("PG", "2.12")  # 2.125 rounds half-even


# --- quotes -------------------------------------------------------------------------------


def test_quotes_keep_request_order_dedupe_uppercase_and_say_why_one_is_missing(market):
    body = market.ok("quotes", symbols=" ko,SPY,zzzz,KO,xom,BRK.B")
    assert [q["symbol"] for q in body["quotes"]] == ["KO", "SPY"]
    assert body["missing"] == [
        {"symbol": "ZZZZ", "reason": "unknown_symbol"},
        {"symbol": "XOM", "reason": "no_bar_at_cutoff"},
        {"symbol": "BRK.B", "reason": "unknown_symbol"},
    ]
    assert body["quotes"][1] == {
        "symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "sector": "Benchmark",
        "close": "505.00", "observed_at": "2026-10-02T20:00:00Z",
        "available_at": "2026-10-02T20:00:00Z", "pct_change_1d": "1.00",
        "pct_change_5d": "1.00", "volume": 9000,
    }  # fmt: skip


def test_nvda_without_a_previous_bar_has_a_null_change_not_zero(market):
    nvda = market.ok("quotes", symbols="NVDA")["quotes"][0]
    assert (nvda["pct_change_1d"], nvda["pct_change_5d"]) == (None, "-20.00")


@pytest.mark.parametrize(
    "symbols",
    [",".join(f"S{n}" for n in range(51)), "AAPL,,KO", "AAPL;KO", "", "1AAPL", "A" * 17],
)
def test_bad_quote_requests_are_422(market, symbols):
    response = market.get("quotes", symbols=symbols)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")


def test_fifty_quotes_are_allowed_and_duplicates_count_once(market):
    fifty = ",".join(f"S{n}" for n in range(50))
    assert len(market.ok("quotes", symbols=fifty)["missing"]) == 50
    assert market.ok("quotes", symbols=fifty + ",S0,s1")["missing"][-1]["symbol"] == "S49"


# --- sectors ------------------------------------------------------------------------------


def test_sectors_are_equal_weighted_and_hand_computed(market):
    body = market.ok("sectors")
    assert body["sectors"] == [
        # (-0.01 + 0.02125) / 2 = 0.005625 -> 0.5625% -> "0.56"
        {"sector": STAPLES, "symbols": 2, "with_bars": 2, "avg_pct_change_1d": "0.56",
         "avg_pct_change_5d": "0.56", "advancers": 1, "decliners": 1, "unchanged": 0},
        {"sector": ENERGY, "symbols": 1, "with_bars": 0, "avg_pct_change_1d": None,
         "avg_pct_change_5d": None, "advancers": 0, "decliners": 0, "unchanged": 0},
        # 1d: (0.05 + 0.05 + 0) / 3, NVDA has none; 5d: (1.10 + 0.05 + 0.05 - 0.20) / 4 = 0.25
        {"sector": IT, "symbols": 4, "with_bars": 4, "avg_pct_change_1d": "3.33",
         "avg_pct_change_5d": "25.00", "advancers": 2, "decliners": 0, "unchanged": 1},
    ]  # fmt: skip
    assert body["benchmark"] == {
        "symbol": "SPY", "close": "505.00", "pct_change_1d": "1.00", "pct_change_5d": "1.00",
        "observed_at": "2026-10-02T20:00:00Z",
    }  # fmt: skip
    assert (body["as_of_session"], body["data_version"]) == ("2026-10-02", BUNDLE)


# --- search -------------------------------------------------------------------------------


def test_search_ranks_exact_symbol_then_prefix_then_name_then_sector(tmp_path):
    members = (
        bundles.Member("CO", "Cooper Companies", "Health Care"),
        bundles.Member("COST", "Costco", STAPLES),
        bundles.Member("ACO", "Acme Cola", "Materials"),
        bundles.Member("KO", "Coca-Cola Company (The)", STAPLES),
        bundles.Member("ZZ", "Zeta", "Consumer Discretionary"),  # "co" only in its sector
        bundles.Member("SPY", "SPDR S&P 500 ETF Trust", "Benchmark", None, benchmark=True),
    )
    build(tmp_path / "m.sqlite3", members=members)
    market = Market(tmp_path / "m.sqlite3")
    market.at(close_at(D0928) - timedelta(days=1))  # search needs no closed session
    body = market.ok("search", q="co")
    assert (body["data_version"], body["query"]) == (BUNDLE, "co")
    assert [r["symbol"] for r in body["results"]] == ["CO", "COST", "ACO", "KO", "ZZ"]
    assert market.ok("search", q="co", limit="2")["results"] == [
        {"symbol": "CO", "name": "Cooper Companies", "sector": "Health Care"},
        {"symbol": "COST", "name": "Costco", "sector": STAPLES},
    ]
    assert market.ok("search", q="BENCH")["results"][0]["symbol"] == "SPY"
    assert market.ok("search", q="nothing")["results"] == []
    market.close()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "   "},
        {"q": "x" * 65},
        {"q": "a", "limit": "26"},
        {"q": "a", "limit": "0"},
    ],
)
def test_bad_searches_are_422(market, params):
    assert market.get("search", **params).status_code == 422


# --- shared rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route, params",
    [
        ("screen", {"as_of": "2026-10-09"}),  # no client as_of, ever
        ("screen", {"sort": "close"}),
        ("screen", {"direction": "up"}),
        ("screen", {"limit": "51"}),
        ("screen", {"limit": "0"}),
        ("screen", {"limit": "ten"}),
        ("screen", {"sector": "Technology"}),  # not a GICS name in the universe
        ("screen", {"sector": "Benchmark"}),
        ("quotes", {"symbols": "AAPL", "cutoff": "2026-10-09T20:00:00Z"}),
        ("sectors", {"sector": IT}),
        ("search", {"q": "a", "as_of": "x"}),
    ],
)
def test_unknown_params_and_bad_values_are_422(market, route, params):
    response = market.get(route, **params)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")


def test_a_repeated_param_is_422(market):
    url = f"/experiments/{market.experiment_id}/market/screen?limit=1&limit=2"
    response = market.http.get(url, headers={"X-Bazaar-Approval": str(market.approval)})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "route, params",
    [("screen", {}), ("quotes", {"symbols": "AAPL"}), ("search", {"q": "a"}), ("sectors", {})],
)
def test_every_route_needs_an_approval_for_this_experiment(market, route, params):
    url = f"/experiments/{market.experiment_id}/market/{route}"
    missing = market.http.get(url, params=params)
    assert (missing.status_code, missing.json()["error"]["code"]) == (401, "unauthorized")
    other = market.approvals.grant(uuid4())
    denied = market.get(route, approval=other, **params)
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "experiment_not_approved")
    # Approval is checked before parameters: a bad request without one is still 401.
    assert market.http.get(url, params={"bogus": "1"}).status_code == 401


def test_an_unknown_experiment_is_404(market):
    other = uuid4()
    approval = market.approvals.grant(other)
    response = market.http.get(
        f"/experiments/{other}/market/sectors", headers={"X-Bazaar-Approval": str(approval)}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")


def test_an_experiment_without_a_universe_is_404(tmp_path):
    build(tmp_path / "m.sqlite3")
    market = Market(tmp_path / "m.sqlite3", data_version=BARS).at(close_at(D1002))
    for route, params in (("screen", {}), ("quotes", {"symbols": "AAPL"}),
                          ("search", {"q": "a"}), ("sectors", {})):  # fmt: skip
        response = market.get(route, **params)
        assert (response.status_code, response.json()["error"]["code"]) == (
            404, "data_unavailable",
        )  # fmt: skip
    market.close()

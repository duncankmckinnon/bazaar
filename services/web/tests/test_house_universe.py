"""The house catalog follows the bundle's universe (S&P 500 on v2), never the window's data."""

import dataclasses
import re
import sqlite3
import time

import bazaar_web.house
import bazaar_web.worker
import httpx
import pytest
from bazaar_web.app import create_app
from bazaar_web.bundle import BundleSource, parse
from bazaar_web.house import LARGE_CAPS, catalog, catalog_for, house_name, universe_catalog
from bazaar_web.store import Store
from fastapi.testclient import TestClient

ADMIN = {"X-Bazaar-Admin-Token": "admin-secret"}
SECTORS = {
    "Information Technology": ["AAPL", "MSFT", "NVDA", "ORCL", "ADBE"],
    "Financials": ["JPM", "V", "MA", "GS", "BRK.B"],
    "Energy": ["XOM", "CVX", "COP"],
    "Consumer Staples": ["KO", "PG", "WMT", "PEP"],
    "Health Care": ["JNJ", "UNH", "LLY", "PFE"],
}
UNIVERSE = [{"symbol": s, "name": f"{s} Corp", "sector": sector}
            for sector, symbols in SECTORS.items() for s in symbols]  # fmt: skip
SYMBOLS = {u["symbol"] for u in UNIVERSE}


def v2_body(universe=UNIVERSE, benchmark="SPY"):
    return {
        "bundle_id": "demo-bundle-v2", "bars": "sp-bars-v2", "news": "n", "filings": "f",
        "start_date": "2026-09-28", "end_date": "2026-10-09",
        "sessions": ["2026-09-28", "2026-10-09"], "incomplete_sessions": [],
        "universe": universe, "universe_name": "S&P 500", "benchmark": benchmark,
    }  # fmt: skip


def tickers_in(text):
    """Upper-case ticker-like tokens (AAPL, BRK.B, V); prose in the templates is lower case."""
    return set(re.findall(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b", text))


# ---------- the catalog ----------


def test_a_universe_gives_sector_large_name_and_benchmark_strategies():
    strategies = catalog_for(parse(v2_body()))
    slugs = [slug for slug, _ in strategies]
    texts = " ".join(text for _, text in strategies)

    for sector in SECTORS:
        assert sector in texts
    assert any(s.endswith("-top-gainers") for s in slugs)
    assert any(s.endswith("-vs-spy") for s in slugs)
    assert "beat-spy" in slugs and "aapl-hold" in slugs and "spy-plus-aapl" in slugs
    # Only the universe's symbols and the benchmark are ever named; EA is not in it.
    assert tickers_in(texts) <= SYMBOLS | {"SPY"}
    assert "EA" not in tickers_in(texts) and "AMZN" not in tickers_in(texts)
    assert {t for t in tickers_in(texts) if t in LARGE_CAPS} == {
        s for s in LARGE_CAPS if s in SYMBOLS
    }
    assert len(slugs) == len(set(slugs))
    assert all(len(house_name(s, 999)) <= 40 for s in slugs)


def test_without_a_benchmark_there_are_no_benchmark_strategies():
    strategies = catalog_for(parse(v2_body(benchmark=None)))
    assert not any("SPY" in text or "spy" in slug for slug, text in strategies)
    assert strategies


def test_the_catalog_is_fixed_and_reads_no_market_data(monkeypatch):
    def no_data(*args, **kwargs):
        raise AssertionError("the house catalog must not read market data")

    monkeypatch.setattr(sqlite3, "connect", no_data)
    universe_catalog.cache_clear()
    first = catalog_for(parse(v2_body()))
    universe_catalog.cache_clear()
    again = catalog_for(parse(v2_body()))
    shuffled = catalog_for(parse(v2_body(universe=list(reversed(UNIVERSE)))))

    assert first == again == shuffled  # inputs are sorted: the universe's order doesn't matter
    assert first[0][0] == "consumer-stapl-top-gainers"  # sorted sectors, fixed template order


def test_v1_and_no_bundle():
    v1 = parse({k: v for k, v in v2_body().items() if k not in ("universe", "benchmark")})
    assert catalog_for(v1) == catalog()  # no universe field: the original 12-stock catalog
    assert catalog_for(None) is None  # no bundle from the route: the lane waits


def test_a_v1_universe_list_without_sectors_still_uses_its_symbols_only():
    body = v2_body(universe=[{"symbol": "AAPL", "name": "Apple"}, {"symbol": "KO"}])
    strategies = catalog_for(parse(body))
    assert tickers_in(" ".join(t for _, t in strategies)) <= {"AAPL", "KO", "SPY"}


# ---------- the cursor and names ----------


def test_the_cursor_is_taken_modulo_a_shorter_catalog(tmp_path):
    store = Store(tmp_path / "web.sqlite3")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE house_state SET cursor = 300, cycle = 1 WHERE id = 1")
    conn.close()
    short = (("a", "Buy KO and hold it all week."), ("b", "Buy XOM and hold it all week."))

    made = [store.create_house(short, None)[1] for _ in range(3)]

    assert made == ["house-a-1", "house-b-1", "house-a-2"]  # 300 % 2 == 0


def test_a_slug_shared_with_an_earlier_catalog_gets_the_next_cycle(tmp_path):
    store = Store(tmp_path / "web.sqlite3")
    old = (("aapl-hold", "Old text."),)
    new = (("aapl-hold", "Put $6,000 into AAPL on day 1 and hold to the end."),)
    store.create_house(old, None)  # house-aapl-hold-1, then the cycle moves to 2
    with sqlite3.connect(store.path) as conn:  # a redeploy that resets the state
        conn.execute("UPDATE house_state SET cursor = 0, cycle = 1 WHERE id = 1")
    conn.close()

    _, name = store.create_house(new, None)

    assert name == "house-aapl-hold-2"


# ---------- the lane ----------


def source(body):
    def make(market_url, bundle_id, **kwargs):
        reply = httpx.Response(404) if body is None else httpx.Response(200, json=body)
        kwargs.setdefault("transport", httpx.MockTransport(lambda request: reply))
        return BundleSource(market_url, bundle_id, **kwargs)

    return make


class AutoRunner:
    def __init__(self, helpers):
        self.helpers = helpers
        self.started = []

    def __call__(self, *, submission_id, name, instructions, runs_dir, **_):
        from bazaar_web.worker import expected_run_dir

        self.started.append((name, instructions))
        return self.helpers.write_run(
            runs_dir, expected_run_dir(runs_dir, submission_id).name,
            policy_ref=f"submission-{name}", period_return="0.0100",
        )  # fmt: skip


@pytest.fixture
def lane(settings, monkeypatch):
    monkeypatch.setattr(bazaar_web.worker, "HOUSE_IDLE_S", 0.02)
    return dataclasses.replace(
        settings, house_enabled=True, house_concurrent=2, data_bundle="demo-bundle-v2"
    )


def test_the_lane_runs_universe_strategies(lane, helpers, monkeypatch):
    monkeypatch.setattr("bazaar_web.app.BundleSource", source(v2_body()))
    runner = AutoRunner(helpers)
    with TestClient(create_app(lane, runner)) as client:
        helpers.wait_for(lambda: len(runner.started) >= 4)
        client.post("/api/admin/house/pause", headers=ADMIN)
        status = client.get("/api/admin/house", headers=ADMIN).json()

    expected = catalog_for(parse(v2_body()))
    assert status["catalog_size"] == len(expected) and status["waiting_for_bundle"] is False
    for name, instructions in runner.started:
        assert tickers_in(instructions) <= SYMBOLS | {"SPY"}
    assert {n for n, _ in runner.started} <= {house_name(s, 1) for s, _ in expected}


@pytest.mark.parametrize("bundle_id", ["demo-bundle-v2", "demo-bundle-v1"])
def test_with_the_bundle_route_down_the_lane_waits(lane, helpers, monkeypatch, bundle_id):
    monkeypatch.setattr("bazaar_web.app.BundleSource", source(None))
    runner = AutoRunner(helpers)
    config = dataclasses.replace(lane, data_bundle=bundle_id)
    with TestClient(create_app(config, runner)) as client:
        time.sleep(0.3)  # many idle rounds
        status = client.get("/api/admin/house", headers=ADMIN).json()
        board = client.get("/api/board").json()

    assert runner.started == []  # no fallback to the 12 (EA, FISV ...) for the house lane
    assert (status["waiting_for_bundle"], status["state"]) == (True, "waiting for bundle")
    assert status["total_started"] == 0
    if bundle_id == "demo-bundle-v1":  # the board still falls back to main's Feb window
        assert board["window"]["start"] == "2026-02-02"

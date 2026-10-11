"""The market routes on an S&P 500-sized synthetic bundle: 504 constituents and SPY, with bars
from 2025-07-01 to 2026-10-09 (about 169,000 rows). Correct at scale, and fast enough."""

import random
import sqlite3
import time
from contextlib import closing
from datetime import date, timedelta
from uuid import uuid4

import pytest
from bazaar_market import bundles, db
from bazaar_market.app import create_app
from bazaar_market.prices import close_at, ensure_schema
from fastapi.testclient import TestClient

from .test_market_routes import TOKEN, Approvals

SECTORS = ("Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy",
           "Financials", "Health Care", "Industrials", "Information Technology", "Materials",
           "Real Estate", "Utilities")  # fmt: skip
MEMBERS = tuple(
    bundles.Member(f"S{n:03d}", f"Company {n}", SECTORS[n % len(SECTORS)]) for n in range(504)
) + (bundles.Member("SPY", "SPDR S&P 500 ETF Trust", "Benchmark", None, benchmark=True),)
BUDGET_MS = 200


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    path = tmp_path_factory.mktemp("scale") / "m.sqlite3"
    db.initialize(path, bundles.SCHEMA)
    with db.write_transaction(path) as connection:
        bundles.seed(connection)
        bundles.store_universe(connection, "demo-bundle-v2", MEMBERS)
    rng = random.Random(505)
    days = [date(2025, 7, 1) + timedelta(n) for n in range(466)]
    rows = []
    for member in MEMBERS:
        price = rng.uniform(20, 500)
        for day in days:
            if day.weekday() < 5:
                price *= 1 + rng.gauss(0, 0.02)
                at = db.format_time(close_at(day))
                close = f"{price:.2f}"
                rows.append(("alpaca-bars-v2", member.symbol, at, at, close, close, close, close,
                             rng.randint(1, 10**8), "synthetic"))  # fmt: skip
    with closing(sqlite3.connect(path)) as connection, connection:
        ensure_schema(connection)
        connection.executemany("INSERT INTO data_bars VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    approvals = Approvals()
    with TestClient(create_app(path, approvals, runner_token=TOKEN)) as http:
        experiment_id = uuid4()
        approval = approvals.grant(experiment_id)
        headers = {"X-Bazaar-Approval": str(approval)}
        http.put(
            f"/experiments/{experiment_id}/cutoff",
            json={"cutoff": close_at(date(2026, 10, 2)).isoformat(),
                  "data_version": "demo-bundle-v2", "execution_rule_version": "exec-v1"},
            headers={**headers, "X-Bazaar-Runner-Token": TOKEN},
        ).raise_for_status()  # fmt: skip
        yield http, f"/experiments/{experiment_id}/market", headers, len(rows)


def timed(http, url, headers, params) -> tuple[float, dict]:
    http.get(url, params=params, headers=headers)  # warm
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        response = http.get(url, params=params, headers=headers)
        samples.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200, response.text
    return sorted(samples)[2], response.json()


@pytest.mark.parametrize(
    "route, params",
    [("screen", {"limit": "50"}), ("screen", {"sort": "pct_change_5d", "sector": "Energy"}),
     ("sectors", {}), ("quotes", {"symbols": ",".join(f"S{n:03d}" for n in range(50))}),
     ("search", {"q": "company 1"})],
)  # fmt: skip
def test_routes_are_correct_and_fast_at_505_symbols(big, route, params, capsys):
    http, base, headers, bars = big
    median, body = timed(http, f"{base}/{route}", headers, params)
    with capsys.disabled():
        print(f"\n  {route} {params if route != 'quotes' else '50 symbols'}: "
              f"median {median:.1f} ms over {bars} bars")  # fmt: skip
    if route == "screen" and "sector" not in params:
        assert len(body["rows"]) == 50 and body["excluded"] == 0
        changes = [float(r["pct_change_1d"]) for r in body["rows"]]
        assert changes == sorted(changes, reverse=True)
    if route == "sectors":
        assert [s["symbols"] for s in body["sectors"]] == [46] * 9 + [45] * 2
        assert sum(s["with_bars"] for s in body["sectors"]) == 504
        assert body["benchmark"]["symbol"] == "SPY"
    if route == "quotes":
        assert len(body["quotes"]) == 50 and body["missing"] == []
    assert median < BUDGET_MS

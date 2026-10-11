"""Concurrent runs against one market database, through the real app.

Run on demand (skipped by default):
    BAZAAR_STRESS=1 uv run pytest services/market/tests/test_stress.py -s
Set BAZAAR_STRESS_RUNS to change the number of concurrent runs (default 15).
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market.app import create_app
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("BAZAAR_STRESS"), reason="stress test: set BAZAAR_STRESS=1 to run"
)

RUNS = int(os.getenv("BAZAAR_STRESS_RUNS", "15"))
TOKEN = "stress-runner-token"
START = date(2025, 7, 1)
DAYS = [d for d in (START + timedelta(days=i) for i in range(30)) if d.weekday() < 5][:10]
SYMBOLS = {"AAPL": Decimal("100.00"), "KO": Decimal("60.00"), "MSFT": Decimal("250.00")}
CASH = Decimal("10000.00")


class AllowAll:
    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return True


def price(symbol: str, day_index: int) -> Decimal:
    return SYMBOLS[symbol] + Decimal(day_index)


@pytest.fixture
def market(tmp_path):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        bars = [
            Bar(symbol=symbol, session=day, open=price(symbol, i), high=price(symbol, i),
                low=price(symbol, i), close=price(symbol, i), volume=1000)
            for symbol in SYMBOLS
            for i, day in enumerate(DAYS)
        ]  # fmt: skip
        import_bars(connection, bars, data_version="stress-v1", source="synthetic")
    app = create_app(path, AllowAll(), runner_token=TOKEN)
    with TestClient(app):  # runs startup once: schemas and bundles
        pass
    return app, path


def run_experiment(app, number: int, results: dict, errors: list) -> None:
    """One runner: cutoff, account, then per day a price read, an order on most days and a
    portfolio read. One order is retried with the same client_order_id."""
    client = TestClient(app)
    headers = {"X-Bazaar-Approval": str(uuid4()), "X-Bazaar-Runner-Token": TOKEN}
    experiment = uuid4()
    url = f"/experiments/{experiment}"
    placed = []
    try:
        for i, day in enumerate(DAYS):
            body = {"cutoff": close_at(day).isoformat()}
            if i == 0:
                body |= {"data_version": "stress-v1", "execution_rule_version": "exec-v1"}
            response = client.put(f"{url}/cutoff", json=body, headers=headers)
            assert response.status_code == 200, response.text
            if i == 0:
                account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
                           "strategy_version_id": str(uuid4()), "cash": str(CASH)}  # fmt: skip
                created = client.post(f"{url}/accounts", json=account, headers=headers)
                assert created.status_code == 201, created.text
                base = f"{url}/accounts/{created.json()['account_id']}"
            symbol = list(SYMBOLS)[(number + i) % len(SYMBOLS)]
            window = {
                "start_at": close_at(DAYS[0]).isoformat(),
                "end_at": close_at(day).isoformat(),
            }
            assert client.get(f"{url}/prices/{symbol}", params=window, headers=headers).is_success
            if (number + i) % 4 != 3:  # 7 or 8 orders per run
                side = "sell" if i >= 6 and placed else "buy"
                order = {"client_order_id": str(uuid4()), "symbol": symbol, "side": side,
                         "quantity": str(1 + (number + i) % 3)}  # fmt: skip
                result = client.post(f"{base}/orders", json=order, headers=headers)
                assert result.status_code == 200, result.text
                placed.append((order, result.json()))
                if i == 4:  # an explicit retry must return the same result, not fill again
                    retry = client.post(f"{base}/orders", json=order, headers=headers)
                    assert retry.json() == result.json()
            portfolio = client.get(f"{base}/portfolio", headers=headers)
            assert portfolio.status_code == 200, portfolio.text
        final = client.get(base, headers=headers).json()
        results[number] = (placed, final)
    except Exception as error:  # noqa: BLE001 -- collected and reported below
        errors.append((number, repr(error)))


def test_concurrent_runs_hold_their_invariants(market):
    app, path = market
    results: dict = {}
    errors: list = []
    threads = [
        threading.Thread(target=run_experiment, args=(app, n, results, errors)) for n in range(RUNS)
    ]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started

    stats = TestClient(app).get("/control/stats", headers={"X-Bazaar-Runner-Token": TOKEN}).json()
    print(f"\n{RUNS} concurrent runs, wall {wall:.2f}s\n{json.dumps(stats, indent=1)}")
    assert errors == []
    assert len(results) == RUNS

    for placed, final in results.values():
        cash, holdings, fills = CASH, {}, 0
        for order, result in placed:
            if result["status"] != "filled":
                continue
            fills += 1
            notional = Decimal(result["unit_price"]) * Decimal(order["quantity"])
            signed = 1 if order["side"] == "buy" else -1
            cash -= signed * notional
            holdings[order["symbol"]] = holdings.get(order["symbol"], 0) + signed * int(
                order["quantity"]
            )
        assert Decimal(final["cash"]) == cash >= 0
        assert {h["symbol"]: int(h["quantity"]) for h in final["holdings"]} == {
            s: q for s, q in holdings.items() if q
        }
        assert all(q >= 0 for q in holdings.values())
        assert final["state_version"] == fills

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM acct_accounts WHERE cash_cents < 0"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM acct_holdings WHERE quantity <= 0"
        ).fetchone() == (0,)
        (orders,) = connection.execute("SELECT COUNT(*) FROM acct_orders").fetchone()
    assert orders == sum(len(placed) for placed, _ in results.values())
    assert stats["sqlite_locked_errors"] == 0
    assert stats["orders_total"] >= orders

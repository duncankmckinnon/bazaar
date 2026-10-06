"""End to end through the real app: real clock, real price store, real ledger."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market.app import create_app
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from fastapi.testclient import TestClient

D1, D2, D3 = date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 3)
APPROVED = uuid4()
CLOSES = {("AAPL", D1): "100.00", ("AAPL", D2): "105.00", ("AAPL", D3): "999.00",
          ("LATE", D3): "50.00"}  # fmt: skip


class AllowOne:
    def allows(self, approval_id: UUID) -> bool:
        return approval_id == APPROVED


def iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def counts(path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("acct_experiments", "acct_accounts", "acct_orders", "acct_fills")
        }


@pytest.fixture
def market(tmp_path):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        bars = [
            Bar(symbol=symbol, session=day, open=Decimal(price), high=Decimal(price),
                low=Decimal(price), close=Decimal(price), volume=1000)
            for (symbol, day), price in CLOSES.items()
        ]  # fmt: skip
        import_bars(connection, bars, data_version="test-v1", source="synthetic")
    with TestClient(create_app(path, AllowOne())) as client:
        yield client, path


def approved() -> dict[str, str]:
    return {"X-Bazaar-Approval": str(APPROVED)}


def buy_or_sell(side: str, quantity: str, symbol: str = "AAPL") -> dict:
    return {"client_order_id": str(uuid4()), "symbol": symbol, "side": side, "quantity": quantity}


def test_one_approved_run_end_to_end(market):
    client, path = market
    eid = uuid4()
    cutoff = {"cutoff": iso(close_at(D1)), "data_version": "test-v1",
              "execution_rule_version": "exec-v1"}  # fmt: skip

    assert client.put(f"/experiments/{eid}/cutoff", json=cutoff).status_code == 401
    denied = client.put(
        f"/experiments/{eid}/cutoff", json=cutoff, headers={"X-Bazaar-Approval": str(uuid4())}
    )
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "experiment_not_approved")
    assert set(counts(path).values()) == {0}

    assert (
        client.put(f"/experiments/{eid}/cutoff", json=cutoff, headers=approved()).status_code == 200
    )
    account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
               "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip
    aid = client.post(f"/experiments/{eid}/accounts", json=account, headers=approved()).json()[
        "account_id"
    ]
    base = f"/experiments/{eid}/accounts/{aid}"

    bought = client.post(f"{base}/orders", json=buy_or_sell("buy", "10"), headers=approved())
    assert bought.json()["status"] == "filled"
    assert (bought.json()["unit_price"], bought.json()["account"]["cash"]) == ("100.00", "9000.00")

    for symbol in ("MSFT", "LATE"):  # no bars at all; first bar after the cutoff
        missing = client.post(f"{base}/orders", json=buy_or_sell("buy", "1", symbol),
                              headers=approved())  # fmt: skip
        assert missing.status_code == 200
        assert missing.json()["status"] == "rejected"
        assert missing.json()["error"]["code"] == "data_unavailable"

    client.put(f"/experiments/{eid}/cutoff", json={"cutoff": iso(close_at(D2))}, headers=approved())
    sold = client.post(f"{base}/orders", json=buy_or_sell("sell", "10"), headers=approved()).json()
    assert sold["unit_price"] == "105.00"
    assert sold["price_observed_at"] == sold["executed_at"] == iso(close_at(D2))
    assert sold["account"]["cash"] == "10050.00"

    portfolio = client.get(f"{base}/portfolio", headers=approved()).json()
    assert (portfolio["portfolio_value"], portfolio["valuation_rule_version"]) == (
        "10050.00",
        "value-v1",
    )
    assert client.post(f"{base}/close", headers=approved()).status_code == 200
    late = client.post(f"{base}/orders", json=buy_or_sell("buy", "1"), headers=approved())
    assert (late.status_code, late.json()["error"]["code"]) == (409, "experiment_not_running")
    assert counts(path)["acct_fills"] == 2


def test_prices_stop_at_the_cutoff(market):
    client, _ = market
    eid = uuid4()
    cutoff = close_at(D2)
    client.put(f"/experiments/{eid}/cutoff", headers=approved(),
               json={"cutoff": iso(cutoff), "data_version": "test-v1",
                     "execution_rule_version": "exec-v1"})  # fmt: skip
    params = {"start_at": iso(close_at(D1)), "end_at": iso(cutoff)}
    at_cutoff = client.get(f"/experiments/{eid}/prices/AAPL", params=params)
    assert at_cutoff.status_code == 200
    observations = at_cutoff.json()["observations"]
    assert [o["price"] for o in observations] == ["100.00", "105.00"]
    assert all(o["available_at"] <= iso(cutoff) for o in observations)

    params["end_at"] = iso(cutoff + timedelta(microseconds=1))
    after = client.get(f"/experiments/{eid}/prices/AAPL", params=params)
    assert (after.status_code, after.json()["error"]["code"]) == (403, "forbidden")
    assert "observations" not in after.json()


@pytest.mark.parametrize("smuggled", ["simulated_at", "cutoff", "executed_at"])
def test_an_order_cannot_carry_its_own_time(market, smuggled):
    client, path = market
    eid = uuid4()
    client.put(f"/experiments/{eid}/cutoff", headers=approved(),
               json={"cutoff": iso(close_at(D1)), "data_version": "test-v1",
                     "execution_rule_version": "exec-v1"})  # fmt: skip
    account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
               "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip
    aid = client.post(f"/experiments/{eid}/accounts", json=account, headers=approved()).json()[
        "account_id"
    ]
    body = {**buy_or_sell("buy", "1"), smuggled: iso(close_at(D3))}
    response = client.post(f"/experiments/{eid}/accounts/{aid}/orders", json=body,
                           headers=approved())  # fmt: skip
    assert response.status_code == 422
    assert counts(path)["acct_orders"] == 0

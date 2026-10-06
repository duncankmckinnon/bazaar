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
TOKEN = "runner-secret"
CLOSES = {("AAPL", D1): "100.00", ("AAPL", D2): "105.00", ("AAPL", D3): "999.00",
          ("LATE", D3): "50.00"}  # fmt: skip


class Approvals:
    """Each approval is good for exactly one experiment."""

    def __init__(self) -> None:
        self.bound: dict[UUID, UUID] = {}

    def grant(self, experiment_id: UUID) -> UUID:
        approval_id = uuid4()
        self.bound[approval_id] = experiment_id
        return approval_id

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return self.bound.get(approval_id) == experiment_id


def iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def counts(path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("acct_experiments", "acct_accounts", "acct_orders", "acct_fills")
        }


def runner(approval) -> dict[str, str]:
    return {"X-Bazaar-Approval": str(approval), "X-Bazaar-Runner-Token": TOKEN}


def agent(approval) -> dict[str, str]:
    return {"X-Bazaar-Approval": str(approval)}


def first_cutoff(day: date) -> dict:
    return {"cutoff": iso(close_at(day)), "data_version": "test-v1",
            "execution_rule_version": "exec-v1"}  # fmt: skip


def account_body() -> dict:
    return {"request_id": str(uuid4()), "agent_id": str(uuid4()),
            "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip


def order(side: str, quantity: str, symbol: str = "AAPL") -> dict:
    return {"client_order_id": str(uuid4()), "symbol": symbol, "side": side, "quantity": quantity}


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
    approvals = Approvals()
    with TestClient(create_app(path, approvals, runner_token=TOKEN)) as client:
        yield client, path, approvals


def start(client, approvals, day: date = D1) -> tuple[UUID, UUID, str]:
    eid = uuid4()
    approval = approvals.grant(eid)
    response = client.put(
        f"/experiments/{eid}/cutoff", json=first_cutoff(day), headers=runner(approval)
    )
    assert response.status_code == 200
    created = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=runner(approval)
    )
    return eid, approval, f"/experiments/{eid}/accounts/{created.json()['account_id']}"


def test_one_approved_run_end_to_end(market):
    client, path, approvals = market
    eid = uuid4()
    approval = approvals.grant(eid)
    cutoff_url = f"/experiments/{eid}/cutoff"

    assert client.put(cutoff_url, json=first_cutoff(D1)).status_code == 401
    denied = client.put(cutoff_url, json=first_cutoff(D1), headers=runner(uuid4()))
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "experiment_not_approved")
    assert set(counts(path).values()) == {0}

    assert (
        client.put(cutoff_url, json=first_cutoff(D1), headers=runner(approval)).status_code == 200
    )
    created = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=runner(approval)
    )
    base = f"/experiments/{eid}/accounts/{created.json()['account_id']}"

    bought = client.post(f"{base}/orders", json=order("buy", "10"), headers=agent(approval)).json()
    assert bought["status"] == "filled"
    assert (bought["unit_price"], bought["account"]["cash"]) == ("100.00", "9000.00")

    for symbol in ("MSFT", "LATE"):  # no bars at all; first bar after the cutoff
        missing = client.post(
            f"{base}/orders", json=order("buy", "1", symbol), headers=agent(approval)
        )
        assert missing.status_code == 200
        assert missing.json()["status"] == "rejected"
        assert missing.json()["error"]["code"] == "data_unavailable"

    advanced = client.put(cutoff_url, json={"cutoff": iso(close_at(D2))}, headers=runner(approval))
    assert advanced.status_code == 200
    sold = client.post(f"{base}/orders", json=order("sell", "10"), headers=agent(approval)).json()
    assert sold["unit_price"] == "105.00"
    assert sold["price_observed_at"] == sold["executed_at"] == iso(close_at(D2))
    assert sold["account"]["cash"] == "10050.00"

    portfolio = client.get(f"{base}/portfolio", headers=agent(approval)).json()
    assert (portfolio["portfolio_value"], portfolio["valuation_rule_version"]) == (
        "10050.00",
        "value-v1",
    )
    assert client.post(f"{base}/close", headers=runner(approval)).status_code == 200
    late = client.post(f"{base}/orders", json=order("buy", "1"), headers=agent(approval))
    assert (late.status_code, late.json()["error"]["code"]) == (409, "experiment_not_running")
    assert counts(path)["acct_fills"] == 2


def test_prices_need_an_approval_for_that_experiment(market):
    client, _, approvals = market
    eid, approval, _ = start(client, approvals, D2)
    url = f"/experiments/{eid}/prices/AAPL"
    params = {"start_at": iso(close_at(D1)), "end_at": iso(close_at(D2))}
    assert client.get(url, params=params).status_code == 401
    other = approvals.grant(uuid4())
    forbidden = client.get(url, params=params, headers=agent(other))
    assert (forbidden.status_code, forbidden.json()["error"]["code"]) == (
        403,
        "experiment_not_approved",
    )
    assert "observations" not in forbidden.json()
    assert client.get(url, params=params, headers=agent(approval)).status_code == 200


def test_prices_stop_at_the_cutoff(market):
    client, _, approvals = market
    eid, approval, _ = start(client, approvals, D2)
    cutoff = close_at(D2)
    url = f"/experiments/{eid}/prices/AAPL"
    params = {"start_at": iso(close_at(D1)), "end_at": iso(cutoff)}
    at_cutoff = client.get(url, params=params, headers=agent(approval))
    assert at_cutoff.status_code == 200
    observations = at_cutoff.json()["observations"]
    assert [o["price"] for o in observations] == ["100.00", "105.00"]
    assert all(o["available_at"] <= iso(cutoff) for o in observations)

    params["end_at"] = iso(cutoff + timedelta(microseconds=1))
    after = client.get(url, params=params, headers=agent(approval))
    assert (after.status_code, after.json()["error"]["code"]) == (403, "forbidden")
    assert "observations" not in after.json()


@pytest.mark.parametrize("smuggled", ["simulated_at", "cutoff", "executed_at"])
def test_an_order_cannot_carry_its_own_time(market, smuggled):
    client, path, approvals = market
    _, approval, base = start(client, approvals)
    body = {**order("buy", "1"), smuggled: iso(close_at(D3))}
    response = client.post(f"{base}/orders", json=body, headers=agent(approval))
    assert response.status_code == 422
    assert counts(path)["acct_orders"] == 0

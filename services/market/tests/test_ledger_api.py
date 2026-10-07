import logging
import sqlite3
from uuid import UUID, uuid4

import pytest
from bazaar_market.ledger import Ledger
from bazaar_market.ledger_api import DenyAllGrants, install
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .ledger_fakes import BARS, FakePrices, catalog

TOKEN = "runner-secret"


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


def client_for(path, grants, runner_token: str | None = TOKEN) -> TestClient:
    ledger = Ledger(path, catalog(FakePrices(BARS)))
    ledger.initialize()
    app = FastAPI()
    install(app, ledger, grants, runner_token)
    return TestClient(app)


def table_counts(path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("acct_experiments", "acct_accounts", "acct_orders", "acct_fills")
        }


CUTOFF = {
    "cutoff": "2025-07-01T20:00:00Z",
    "data_version": "fixture-v1",
    "execution_rule_version": "exec-v1",
}


def account_body() -> dict:
    return {
        "request_id": str(uuid4()),
        "agent_id": str(uuid4()),
        "strategy_version_id": str(uuid4()),
        "cash": "1000.00",
    }


def runner(approval) -> dict[str, str]:
    return {"X-Bazaar-Approval": str(approval), "X-Bazaar-Runner-Token": TOKEN}


def agent(approval) -> dict[str, str]:
    return {"X-Bazaar-Approval": str(approval)}


def start(client, approvals) -> tuple[UUID, UUID, str]:
    eid = uuid4()
    approval = approvals.grant(eid)
    response = client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=runner(approval))
    assert response.is_success
    created = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=runner(approval)
    )
    return eid, approval, f"/experiments/{eid}/accounts/{created.json()['account_id']}"


def test_missing_approval_is_401(tmp_path):
    client = client_for(tmp_path / "m.db", Approvals())
    response = client.put(
        f"/experiments/{uuid4()}/cutoff", json=CUTOFF, headers={"X-Bazaar-Runner-Token": TOKEN}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("bound", [False, True])
def test_denied_cutoff_and_account_write_nothing_then_an_allowed_retry_works(
    tmp_path, caplog, bound
):
    caplog.set_level(logging.INFO, logger="bazaar_market.ledger_api")
    approvals = Approvals()
    client = client_for(tmp_path / "m.db", approvals if bound else DenyAllGrants())
    eid = uuid4()
    for_other = approvals.grant(uuid4())
    body = account_body()

    for response in (
        client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=runner(uuid4())),
        client.post(f"/experiments/{eid}/accounts", json=body, headers=runner(uuid4())),
        client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=runner("not-a-uuid")),
        client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=runner(for_other)),
        client.post(f"/experiments/{eid}/accounts", json=body, headers=runner(for_other)),
    ):
        assert response.status_code == 403
        assert response.json() == {
            "error": {
                "code": "experiment_not_approved",
                "message": "This approval does not allow the call",
                "retryable": False,
            }
        }
    assert set(table_counts(tmp_path / "m.db").values()) == {0}
    assert f"approval denied: approval_id={for_other} experiment_id={eid}" in caplog.text

    if bound:
        approval = approvals.grant(eid)
        cutoff = client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=runner(approval))
        assert cutoff.is_success
        created = client.post(f"/experiments/{eid}/accounts", json=body, headers=runner(approval))
        assert created.status_code == 201
        assert f"approval allowed: approval_id={approval} experiment_id={eid}" in caplog.text


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_control_routes_need_the_runner_token(tmp_path, caplog, token):
    caplog.set_level(logging.INFO, logger="bazaar_market.ledger_api")
    approvals = Approvals()
    client = client_for(tmp_path / "m.db", approvals)
    eid = uuid4()
    headers = agent(approvals.grant(eid))
    if token is not None:
        headers["X-Bazaar-Runner-Token"] = token
    for response in (
        client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers),
        client.post(f"/experiments/{eid}/accounts", json=account_body(), headers=headers),
        client.post(f"/experiments/{eid}/accounts/{uuid4()}/close", headers=headers),
    ):
        assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthorized")
    assert set(table_counts(tmp_path / "m.db").values()) == {0}
    assert "runner token refused" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("configured", [None, ""])
def test_control_routes_refuse_everything_without_a_configured_token(tmp_path, configured):
    approvals = Approvals()
    client = client_for(tmp_path / "m.db", approvals, runner_token=configured)
    eid = uuid4()
    approval = approvals.grant(eid)
    for sent in (TOKEN, ""):
        headers = {"X-Bazaar-Approval": str(approval), "X-Bazaar-Runner-Token": sent}
        response = client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers)
        assert response.status_code == 401
    assert set(table_counts(tmp_path / "m.db").values()) == {0}


def test_the_runner_token_does_not_replace_an_approval_on_orders(tmp_path):
    approvals = Approvals()
    client = client_for(tmp_path / "m.db", approvals)
    _, approval, base = start(client, approvals)
    order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "1"}
    token_only = {"X-Bazaar-Runner-Token": TOKEN}
    assert client.post(f"{base}/orders", json=order, headers=token_only).status_code == 401
    wrong = {**token_only, "X-Bazaar-Approval": str(approvals.grant(uuid4()))}
    assert client.post(f"{base}/orders", json=order, headers=wrong).status_code == 403
    assert client.get(base, headers=wrong).status_code == 403
    assert table_counts(tmp_path / "m.db")["acct_orders"] == 0
    assert client.post(f"{base}/orders", json=order, headers=agent(approval)).status_code == 200


def test_account_flow_over_http(tmp_path):
    approvals = Approvals()
    client = client_for(tmp_path / "m.db", approvals)
    eid = uuid4()
    approval = approvals.grant(eid)
    early = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=runner(approval)
    )
    assert early.status_code == 409
    assert client.put(
        f"/experiments/{eid}/cutoff", json=CUTOFF, headers=runner(approval)
    ).json() == {"experiment_id": str(eid), "cutoff": "2025-07-01T20:00:00Z"}
    created = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=runner(approval)
    )
    base = f"/experiments/{eid}/accounts/{created.json()['account_id']}"
    order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "3"}
    filled = client.post(f"{base}/orders", json=order, headers=agent(approval)).json()
    assert (filled["status"], filled["account"]["cash"]) == ("filled", "700.00")
    too_big = {**order, "client_order_id": str(uuid4()), "quantity": "100"}
    rejected = client.post(f"{base}/orders", json=too_big, headers=agent(approval))
    assert rejected.status_code == 200
    assert rejected.json()["error"]["code"] == "insufficient_cash"
    fractional = {**order, "client_order_id": str(uuid4()), "quantity": "0.5"}
    response = client.post(f"{base}/orders", json=fractional, headers=agent(approval))
    assert response.status_code == 422
    portfolio = client.get(f"{base}/portfolio", headers=agent(approval)).json()
    assert portfolio["portfolio_value"] == "1000.00"
    assert client.post(f"{base}/close", headers=agent(approval)).status_code == 401
    assert client.post(f"{base}/close", headers=runner(approval)).status_code == 200
    late = {**order, "client_order_id": str(uuid4())}
    closed = client.post(f"{base}/orders", json=late, headers=agent(approval))
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "experiment_not_running"

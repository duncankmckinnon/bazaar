import logging
import sqlite3
from uuid import UUID, uuid4

import pytest
from bazaar_market.ledger import Ledger
from bazaar_market.ledger_api import DenyAllGrants, install
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .ledger_fakes import BARS, FakePrices, catalog

APPROVED = uuid4()


class AllowOne:
    def allows(self, approval_id: UUID) -> bool:
        return approval_id == APPROVED


def client_for(path, grants) -> TestClient:
    ledger = Ledger(path, catalog(FakePrices(BARS)))
    ledger.initialize()
    app = FastAPI()
    install(app, ledger, grants)
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


def headers(approval=APPROVED) -> dict[str, str]:
    return {"X-Bazaar-Approval": str(approval)}


def test_missing_approval_is_401(tmp_path):
    client = client_for(tmp_path / "m.db", AllowOne())
    response = client.put(f"/experiments/{uuid4()}/cutoff", json=CUTOFF)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("grants", [DenyAllGrants(), AllowOne()])
def test_denied_cutoff_and_account_write_nothing_then_an_allowed_retry_works(
    tmp_path, caplog, grants
):
    caplog.set_level(logging.INFO, logger="bazaar_market.ledger_api")
    client = client_for(tmp_path / "m.db", grants)
    eid, body = uuid4(), account_body()
    denied = uuid4()

    for response in (
        client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers(denied)),
        client.post(f"/experiments/{eid}/accounts", json=body, headers=headers(denied)),
        client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers("not-a-uuid")),
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
    assert f"approval denied: approval_id={denied}" in caplog.text

    if isinstance(grants, AllowOne):
        assert (
            client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers()).status_code
            == 200
        )
        created = client.post(f"/experiments/{eid}/accounts", json=body, headers=headers())
        assert created.status_code == 201
        assert f"approval allowed: approval_id={APPROVED}" in caplog.text


def test_orders_and_reads_need_approval_too(tmp_path):
    client = client_for(tmp_path / "m.db", AllowOne())
    eid = uuid4()
    client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers())
    aid = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=headers()
    ).json()["account_id"]
    order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "1"}
    base = f"/experiments/{eid}/accounts/{aid}"
    assert client.post(f"{base}/orders", json=order, headers=headers(uuid4())).status_code == 403
    assert client.get(base, headers=headers(uuid4())).status_code == 403
    assert table_counts(tmp_path / "m.db")["acct_orders"] == 0


def test_account_flow_over_http(tmp_path):
    client = client_for(tmp_path / "m.db", AllowOne())
    eid = uuid4()
    assert (
        client.post(
            f"/experiments/{eid}/accounts", json=account_body(), headers=headers()
        ).status_code
        == 409
    )
    assert client.put(f"/experiments/{eid}/cutoff", json=CUTOFF, headers=headers()).json() == {
        "experiment_id": str(eid),
        "cutoff": "2025-07-01T20:00:00Z",
    }
    aid = client.post(
        f"/experiments/{eid}/accounts", json=account_body(), headers=headers()
    ).json()["account_id"]
    base = f"/experiments/{eid}/accounts/{aid}"
    order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "3"}
    filled = client.post(f"{base}/orders", json=order, headers=headers()).json()
    assert (filled["status"], filled["account"]["cash"]) == ("filled", "700.00")
    too_big = {**order, "client_order_id": str(uuid4()), "quantity": "100"}
    rejected = client.post(f"{base}/orders", json=too_big, headers=headers())
    assert rejected.status_code == 200
    assert rejected.json()["error"]["code"] == "insufficient_cash"
    fractional = {**order, "client_order_id": str(uuid4()), "quantity": "0.5"}
    assert client.post(f"{base}/orders", json=fractional, headers=headers()).status_code == 422
    assert client.get(f"{base}/portfolio", headers=headers()).json()["portfolio_value"] == "1000.00"
    assert client.post(f"{base}/close", headers=headers()).status_code == 200
    late = {**order, "client_order_id": str(uuid4())}
    closed = client.post(f"{base}/orders", json=late, headers=headers())
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "experiment_not_running"

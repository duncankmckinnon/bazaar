"""Runner-recorded grants, through the real app with its default SqliteGrants checker."""

import logging
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from bazaar_market.app import create_app
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from fastapi.testclient import TestClient

TOKEN = "runner-secret"
DAY = date(2025, 7, 1)
CUTOFF = {
    "cutoff": close_at(DAY).isoformat(),
    "data_version": "test-v1",
    "execution_rule_version": "exec-v1",
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        price = Decimal("100.00")
        bar = Bar(symbol="AAPL", session=DAY, open=price, high=price, low=price, close=price,
                  volume=1000)  # fmt: skip
        import_bars(connection, [bar], data_version="test-v1", source="synthetic")
    return path


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path, runner_token=TOKEN)) as client:
        yield client


def grant(client, approval, experiment, token=TOKEN):
    headers = {} if token is None else {"X-Bazaar-Runner-Token": token}
    body = {"approval_id": str(approval), "experiment_id": str(experiment)}
    return client.post("/control/grants", json=body, headers=headers)


def grants_rows(path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM acct_grants").fetchone()[0]


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_a_grant_needs_the_runner_token(client, db_path, token, caplog):
    caplog.set_level(logging.INFO, logger="bazaar_market")
    response = grant(client, uuid4(), uuid4(), token=token)
    assert (response.status_code, response.json()["error"]["code"]) == (401, "unauthorized")
    assert grants_rows(db_path) == 0
    assert TOKEN not in caplog.text


def test_an_approval_header_cannot_record_a_grant(client, db_path):
    approval, experiment = uuid4(), uuid4()
    response = client.post(
        "/control/grants",
        json={"approval_id": str(approval), "experiment_id": str(experiment)},
        headers={"X-Bazaar-Approval": str(approval)},
    )
    assert response.status_code == 401
    assert grants_rows(db_path) == 0


@pytest.mark.parametrize("configured", [None, ""])
def test_no_configured_token_refuses_every_grant(db_path, configured, monkeypatch):
    monkeypatch.delenv("BAZAAR_RUNNER_TOKEN", raising=False)
    with TestClient(create_app(db_path, runner_token=configured)) as client:
        for sent in (TOKEN, ""):
            assert grant(client, uuid4(), uuid4(), token=sent).status_code == 401
    assert grants_rows(db_path) == 0


def test_the_same_grant_twice_is_204_and_rebinding_is_409(client, db_path, caplog):
    caplog.set_level(logging.INFO, logger="bazaar_market")
    approval, experiment = uuid4(), uuid4()
    assert grant(client, approval, experiment).status_code == 204
    assert grant(client, approval, experiment).status_code == 204
    conflict = grant(client, approval, uuid4())
    assert (conflict.status_code, conflict.json()["error"]["code"]) == (409, "conflict")
    assert grants_rows(db_path) == 1
    assert f"grant created: approval_id={approval} experiment_id={experiment}" in caplog.text
    assert caplog.text.count("grant created") == 1


@pytest.mark.parametrize(
    "body",
    [
        {"approval_id": "sub-123", "experiment_id": str(uuid4())},
        {"approval_id": str(uuid4()), "experiment_id": "not-a-uuid"},
        {"approval_id": str(uuid4())},
        {"approval_id": str(uuid4()), "experiment_id": str(uuid4()), "extra": "x"},
    ],
)
def test_ids_must_be_uuids(client, db_path, body):
    response = client.post("/control/grants", json=body, headers={"X-Bazaar-Runner-Token": TOKEN})
    assert response.status_code == 422
    assert grants_rows(db_path) == 0


def run_headers(approval) -> dict[str, str]:
    return {"X-Bazaar-Approval": str(approval), "X-Bazaar-Runner-Token": TOKEN}


def test_a_granted_approval_runs_its_experiment_and_only_that_one(client):
    approval, experiment = uuid4(), uuid4()
    url = f"/experiments/{experiment}/cutoff"
    assert client.put(url, json=CUTOFF, headers=run_headers(approval)).status_code == 403

    assert grant(client, approval, experiment).status_code == 204
    assert client.put(url, json=CUTOFF, headers=run_headers(approval)).status_code == 200
    account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
               "strategy_version_id": str(uuid4()), "cash": "1000.00"}  # fmt: skip
    created = client.post(f"/experiments/{experiment}/accounts", json=account,
                          headers=run_headers(approval))  # fmt: skip
    assert created.status_code == 201
    order = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "1"}
    filled = client.post(
        f"/experiments/{experiment}/accounts/{created.json()['account_id']}/orders",
        json=order,
        headers={"X-Bazaar-Approval": str(approval)},
    )
    assert filled.json()["status"] == "filled"

    other = client.put(f"/experiments/{uuid4()}/cutoff", json=CUTOFF, headers=run_headers(approval))
    assert (other.status_code, other.json()["error"]["code"]) == (403, "experiment_not_approved")


def test_a_grant_survives_a_restart(db_path):
    approval, experiment = uuid4(), uuid4()
    with TestClient(create_app(db_path, runner_token=TOKEN)) as client:
        assert grant(client, approval, experiment).status_code == 204
    with TestClient(create_app(db_path, runner_token=TOKEN)) as client:
        url = f"/experiments/{experiment}/cutoff"
        assert client.put(url, json=CUTOFF, headers=run_headers(approval)).status_code == 200


def test_the_token_is_checked_before_the_body(client):
    response = client.post("/control/grants", json={"approval_id": "x"})
    assert response.status_code == 401

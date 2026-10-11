import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market.app import create_app
from bazaar_market.ledger import Ledger
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from fastapi.testclient import TestClient

TOKEN = "stats-runner-token"
DAY = date(2025, 7, 1)


class AllowAll:
    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return True


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "m.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        p = Decimal("100.00")
        bar = Bar(symbol="AAPL", session=DAY, open=p, high=p, low=p, close=p, volume=1000)
        import_bars(connection, [bar], data_version="test-v1", source="synthetic")
    app = create_app(path, AllowAll(), runner_token=TOKEN)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def stats(client) -> dict:
    response = client.get("/control/stats", headers={"X-Bazaar-Runner-Token": TOKEN})
    assert response.status_code == 200
    return response.json()


def start(client) -> tuple[str, str]:
    eid, approval = uuid4(), uuid4()
    headers = {"X-Bazaar-Approval": str(approval), "X-Bazaar-Runner-Token": TOKEN}
    body = {"cutoff": close_at(DAY).isoformat(), "data_version": "test-v1",
            "execution_rule_version": "exec-v1"}  # fmt: skip
    assert client.put(f"/experiments/{eid}/cutoff", json=body, headers=headers).is_success
    account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
               "strategy_version_id": str(uuid4()), "cash": "1000.00"}  # fmt: skip
    created = client.post(f"/experiments/{eid}/accounts", json=account, headers=headers).json()
    return f"/experiments/{eid}/accounts/{created['account_id']}", str(approval)


def order(client, base, approval):
    body = {"client_order_id": str(uuid4()), "symbol": "AAPL", "side": "buy", "quantity": "1"}
    return client.post(f"{base}/orders", json=body, headers={"X-Bazaar-Approval": approval})


def test_the_shape(client):
    snapshot = stats(client)
    assert set(snapshot) == {
        "started_at",
        "write_lock_wait_ms",
        "write_lock_waits_over_100ms",
        "sqlite_locked_errors",
        "order_latency_ms",
        "orders_total",
    }
    assert set(snapshot["write_lock_wait_ms"]) == {"p50", "p95", "max"}
    assert set(snapshot["order_latency_ms"]) == {"p50", "p95"}


@pytest.mark.parametrize("token", [None, "wrong"])
def test_stats_need_the_runner_token(client, token):
    headers = {} if token is None else {"X-Bazaar-Runner-Token": token}
    assert client.get("/control/stats", headers=headers).status_code == 401


def test_no_configured_token_refuses_stats(tmp_path, monkeypatch):
    monkeypatch.delenv("BAZAAR_RUNNER_TOKEN", raising=False)
    with TestClient(create_app(tmp_path / "m.sqlite3", runner_token="")) as client:
        response = client.get("/control/stats", headers={"X-Bazaar-Runner-Token": ""})
        assert response.status_code == 401


def test_orders_and_lock_waits_are_counted(client):
    before = stats(client)
    base, approval = start(client)
    assert order(client, base, approval).json()["status"] == "filled"
    assert order(client, base, approval).json()["status"] == "filled"
    after = stats(client)
    assert after["orders_total"] == before["orders_total"] + 2
    assert after["order_latency_ms"]["p50"] is not None
    assert after["write_lock_wait_ms"]["max"] is not None


def test_a_locked_database_is_counted_and_still_fails(client, monkeypatch):
    base, approval = start(client)
    before = stats(client)["sqlite_locked_errors"]

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Ledger, "submit", locked)
    assert order(client, base, approval).status_code == 500
    after = stats(client)
    assert after["sqlite_locked_errors"] == before + 1
    assert after["orders_total"] >= 1


def test_stats_carry_no_ids_symbols_or_token(client):
    base, approval = start(client)
    order(client, base, approval)
    text = repr(stats(client))
    for value in (TOKEN, approval, "AAPL", *base.split("/")[2::2]):
        assert value not in text

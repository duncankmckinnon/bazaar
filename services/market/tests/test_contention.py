"""Many writers on ONE account at the same instant, through the real app.

Each scenario starts 30 threads, each with its own client, held at a barrier so every POST is in
flight together. Admission and commit share one BEGIN IMMEDIATE transaction, so exactly the
orders the account can afford fill and the rest are rejected, never more.
"""

import sqlite3
import threading
from contextlib import closing
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market import db
from bazaar_market.app import create_app
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from fastapi.testclient import TestClient

TOKEN = "contention-runner-token"
DAY = date(2025, 7, 1)
WRITERS = 30


class AllowAll:
    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return True


def seed(path, prices: dict[str, str]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        ensure_schema(connection)
        bars = [
            Bar(symbol=symbol, session=DAY, open=Decimal(p), high=Decimal(p), low=Decimal(p),
                close=Decimal(p), volume=1000)
            for symbol, p in prices.items()
        ]  # fmt: skip
        import_bars(connection, bars, data_version="test-v1", source="synthetic")


@pytest.fixture
def market(tmp_path):
    path = tmp_path / "market.sqlite3"
    seed(path, {"BIG": "500.00", "AAPL": "100.00"})
    app = create_app(path, AllowAll(), runner_token=TOKEN)
    with TestClient(app) as client:
        experiment = uuid4()
        headers = {"X-Bazaar-Approval": str(uuid4()), "X-Bazaar-Runner-Token": TOKEN}
        body = {"cutoff": close_at(DAY).isoformat(), "data_version": "test-v1",
                "execution_rule_version": "exec-v1"}  # fmt: skip
        assert client.put(
            f"/experiments/{experiment}/cutoff", json=body, headers=headers
        ).is_success
        account = {"request_id": str(uuid4()), "agent_id": str(uuid4()),
                   "strategy_version_id": str(uuid4()), "cash": "10000.00"}  # fmt: skip
        created = client.post(f"/experiments/{experiment}/accounts", json=account, headers=headers)
        base = f"/experiments/{experiment}/accounts/{created.json()['account_id']}"
        yield app, client, base, headers, path


def order(symbol: str, side: str, quantity: str, client_order_id: str | None = None) -> dict:
    return {"client_order_id": client_order_id or str(uuid4()), "symbol": symbol, "side": side,
            "quantity": quantity}  # fmt: skip


def all_at_once(app, base: str, headers: dict, bodies: list[dict]) -> list:
    """POST every body from its own thread, all released together. Returns the responses."""
    barrier = threading.Barrier(len(bodies))
    responses: list = [None] * len(bodies)

    def post(i: int) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        barrier.wait()
        responses[i] = client.post(f"{base}/orders", json=bodies[i], headers=headers)

    threads = [threading.Thread(target=post, args=(i,)) for i in range(len(bodies))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return responses


def outcomes(responses: list) -> list[str]:
    assert [r.status_code for r in responses] == [200] * len(responses), [
        r.text for r in responses if r.status_code != 200
    ]
    return [r.json()["status"] if r.json()["status"] == "filled" else r.json()["error"]["code"]
            for r in responses]  # fmt: skip


def test_concurrent_buys_never_spend_more_than_the_cash(market):
    app, client, base, headers, _ = market
    responses = all_at_once(app, base, headers, [order("BIG", "buy", "1") for _ in range(WRITERS)])
    results = outcomes(responses)
    assert results.count("filled") == 20
    assert results.count("insufficient_cash") == 10
    account = client.get(base, headers=headers).json()
    assert account["cash"] == "0.00"
    assert account["holdings"] == [{"symbol": "BIG", "quantity": "20"}]
    assert account["state_version"] == 20


def test_concurrent_sells_never_sell_more_than_is_held(market):
    app, client, base, headers, _ = market
    bought = client.post(f"{base}/orders", json=order("AAPL", "buy", "100"), headers=headers)
    assert bought.json()["status"] == "filled"
    responses = all_at_once(
        app, base, headers, [order("AAPL", "sell", "5") for _ in range(WRITERS)]
    )
    results = outcomes(responses)
    assert results.count("filled") == 20
    assert results.count("insufficient_holdings") == 10
    account = client.get(base, headers=headers).json()
    assert account["holdings"] == []
    assert account["cash"] == "10000.00"


def test_concurrent_posts_of_one_order_fill_it_once(market):
    app, client, base, headers, path = market
    same = order("BIG", "buy", "1", client_order_id=str(uuid4()))
    responses = all_at_once(app, base, headers, [dict(same) for _ in range(WRITERS)])
    assert outcomes(responses) == ["filled"] * WRITERS
    assert len({r.json()["order_id"] for r in responses}) == 1
    assert client.get(base, headers=headers).json()["cash"] == "9500.00"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM acct_orders").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM acct_fills").fetchone() == (1,)


def test_read_only_connections_open_paths_with_spaces_and_special_characters(tmp_path):
    folder = tmp_path / "market data #1"
    folder.mkdir()
    path = folder / "market.sqlite3"
    seed(path, {"AAPL": "100.00"})
    with closing(db.connect(path, read_only=True)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM data_bars").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM data_bars")

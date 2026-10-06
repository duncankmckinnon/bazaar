"""The research routes, read through Duncan's ResearchTools client against the real app."""

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from bazaar_agent.research import ResearchContext, ResearchTools
from bazaar_market.app import create_app
from bazaar_market.prices import Bar, close_at, ensure_schema, import_bars
from bazaar_protocol import ExperimentContext
from bazaar_protocol.research import HistoryRequest
from fastapi.testclient import TestClient

D1, D2, D3 = date(2025, 7, 1), date(2025, 7, 2), date(2025, 7, 3)
TOKEN = "runner-secret"
CLOSES = {"AAPL": ["100.00", "105.00", "110.00"], "KO": ["60.00", "61.00", "62.00"]}


class Approvals:
    def __init__(self) -> None:
        self.bound: dict[UUID, UUID] = {}

    def grant(self, experiment_id: UUID) -> UUID:
        approval_id = uuid4()
        self.bound[approval_id] = experiment_id
        return approval_id

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return self.bound.get(approval_id) == experiment_id


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class Run:
    """One experiment and account, driven over HTTP the way the runner and agent would."""

    def __init__(self, tmp_path, bars_version="test-v1", data_version="test-v1") -> None:
        path = tmp_path / "market.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            ensure_schema(connection)
            bars = [
                Bar(symbol=symbol, session=day, open=Decimal(price), high=Decimal(price),
                    low=Decimal(price), close=Decimal(price), volume=1000)
                for symbol, prices in CLOSES.items()
                for day, price in zip((D1, D2, D3), prices, strict=True)
            ]  # fmt: skip
            import_bars(connection, bars, data_version=bars_version, source="synthetic")
        self.data_version = data_version
        self.approvals = Approvals()
        self.app = create_app(path, self.approvals, runner_token=TOKEN)
        self.http = TestClient(self.app)
        self.http.__enter__()
        self.experiment_id = uuid4()
        self.approval = self.approvals.grant(self.experiment_id)
        self.agent_id, self.strategy_version_id = uuid4(), uuid4()
        self.cutoff(D1, first=True)
        created = self.http.post(
            f"/experiments/{self.experiment_id}/accounts",
            headers=self.runner_headers,
            json={"request_id": str(uuid4()), "agent_id": str(self.agent_id),
                  "strategy_version_id": str(self.strategy_version_id), "cash": "1000.00"},
        )  # fmt: skip
        self.account_id = UUID(created.json()["account_id"])

    @property
    def runner_headers(self) -> dict[str, str]:
        return {"X-Bazaar-Approval": str(self.approval), "X-Bazaar-Runner-Token": TOKEN}

    @property
    def base(self) -> str:
        return f"/experiments/{self.experiment_id}/accounts/{self.account_id}"

    def cutoff(self, day: date, first: bool = False) -> None:
        body = {"cutoff": iso(close_at(day))}
        if first:
            body |= {"data_version": self.data_version, "execution_rule_version": "exec-v1"}
        url = f"/experiments/{self.experiment_id}/cutoff"
        assert self.http.put(url, json=body, headers=self.runner_headers).status_code == 200
        self.now = close_at(day)

    def order(self, side: str, quantity: str, symbol: str = "AAPL") -> dict:
        body = {"client_order_id": str(uuid4()), "symbol": symbol, "side": side,
                "quantity": quantity}  # fmt: skip
        headers = {"X-Bazaar-Approval": str(self.approval)}
        return self.http.post(f"{self.base}/orders", json=body, headers=headers).json()

    def tools(self, approval: UUID | None = None) -> tuple[ResearchTools, httpx.AsyncClient]:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://market",
            headers={"X-Bazaar-Approval": str(approval or self.approval)},
        )
        context = ExperimentContext(
            experiment_id=self.experiment_id,
            agent_id=self.agent_id,
            account_id=self.account_id,
            strategy_version_id=self.strategy_version_id,
            approval_id=self.approval,
            simulated_at=self.now,
            event_sequence=0,
            data_version=self.data_version,
            execution_rule_version="exec-v1",
        )
        return ResearchTools(client, ResearchContext(experiment=context)), client

    def close(self) -> None:
        self.http.__exit__(None, None, None)


@pytest.fixture
def run(tmp_path):
    run = Run(tmp_path)
    yield run
    run.close()


def window(run: Run, start: date = D1, **fields) -> HistoryRequest:
    return HistoryRequest(start_at=close_at(start), end_at=run.now, **fields)


async def test_order_history_pages_through_the_client(run):
    placed = [run.order("buy", "2"), run.order("buy", "100")]  # filled, rejected
    run.cutoff(D2)
    placed += [run.order("sell", "1"), run.order("buy", "3", symbol="KO")]
    tools, client = run.tools()
    async with client:
        first = await tools.orders(window(run, limit=3))
        assert first.error is None
        assert len(first.data.items) == 3 and first.data.next_cursor is not None
        assert first.data.source == "market-ledger-v1"
        second = await tools.orders(window(run, limit=3, cursor=first.data.next_cursor))
        assert second.error is None
        assert second.data.next_cursor is None

    returned = [*first.data.items, *second.data.items]
    assert sorted(str(item.order_id) for item in returned) == sorted(p["order_id"] for p in placed)
    assert {item.status for item in returned} == {"filled", "rejected"}
    by_id = {str(item.order_id): item for item in returned}
    # Filled results come back exactly as first returned; rejections differ only in the
    # message, which the client replaces with the error code.
    for result in placed:
        item = by_id[result["order_id"]].model_dump(mode="json")
        if result["status"] == "rejected":
            item["error"]["message"] = result["error"]["message"]
        assert item == result


async def test_order_history_respects_the_window(run):
    run.order("buy", "1")
    run.cutoff(D2)
    later = run.order("buy", "1")
    tools, client = run.tools()
    async with client:
        result = await tools.orders(window(run, start=D2))
    assert result.error is None
    assert [str(item.order_id) for item in result.data.items] == [later["order_id"]]


async def test_order_history_needs_an_approval_for_that_experiment(run):
    run.order("buy", "1")
    tools, client = run.tools(approval=run.approvals.grant(uuid4()))
    async with client:
        result = await tools.orders(window(run))
    assert (result.data, result.error.code) == (None, "unauthorized")


def test_order_history_refuses_future_windows_and_bad_cursors(run):
    headers = {"X-Bazaar-Approval": str(run.approval)}
    url = f"{run.base}/orders"
    future = {"start_at": iso(close_at(D1)), "end_at": iso(run.now + timedelta(microseconds=1))}
    response = run.http.get(url, params=future, headers=headers)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "forbidden")
    bad = {"start_at": iso(close_at(D1)), "end_at": iso(run.now), "cursor": "abc.def"}
    assert run.http.get(url, params=bad, headers=headers).status_code == 422
    missing = run.http.get(url, params={"end_at": iso(run.now)}, headers=headers)
    assert (missing.status_code, missing.json()["error"]["code"]) == (422, "invalid_request")


async def test_a_bundle_experiment_reports_the_bundle_and_prices_from_its_bars(tmp_path):
    run = Run(tmp_path, bars_version="alpaca-bars-v1", data_version="demo-bundle-v1")
    try:
        filled = run.order("buy", "2")
        assert (filled["status"], filled["unit_price"]) == ("filled", "100.00")
        assert filled["data_version"] == "demo-bundle-v1"
        portfolio = run.http.get(
            f"{run.base}/portfolio", headers={"X-Bazaar-Approval": str(run.approval)}
        ).json()
        assert portfolio["data_version"] == "demo-bundle-v1"
        assert run.app.state.component(run.experiment_id, "news") == "alpaca-news-v1"
        assert run.app.state.component(run.experiment_id, "bars") == "alpaca-bars-v1"
        tools, client = run.tools()
        async with client:
            orders = await tools.orders(window(run))
            current = await tools.portfolio()
        assert orders.error is None and orders.data.data_version == "demo-bundle-v1"
        assert current.error is None
    finally:
        run.close()


def test_a_plain_bars_experiment_has_no_news_component(run):
    from bazaar_market.bundles import NoComponent

    assert run.app.state.component(run.experiment_id, "bars") == "test-v1"
    with pytest.raises(NoComponent):
        run.app.state.component(run.experiment_id, "news")


def test_research_scope_reads_the_account_header(run):
    from typing import Annotated

    from bazaar_market.history import PageScope
    from bazaar_market.ledger_api import research_scope
    from fastapi import Depends

    @run.app.get("/experiments/{experiment_id}/probe")
    def probe(scope: Annotated[PageScope, Depends(research_scope)]) -> dict[str, str]:
        return {"account_id": str(scope.account_id), "data_version": scope.data_version,
                "agent_id": str(scope.agent_id), "cutoff_at": iso(scope.cutoff_at)}  # fmt: skip

    url = f"/experiments/{run.experiment_id}/probe"
    missing = run.http.get(url)
    assert (missing.status_code, missing.json()["error"]["code"]) == (401, "unauthorized")
    for account in (str(uuid4()), "not-a-uuid"):
        foreign = run.http.get(url, headers={"X-Bazaar-Account": account})
        assert (foreign.status_code, foreign.json()["error"]["code"]) == (403, "forbidden")
    (other_dir := run.app.state.market_db_path.parent / "other").mkdir()
    other = Run(other_dir)
    try:
        elsewhere = run.http.get(url, headers={"X-Bazaar-Account": str(other.account_id)})
        assert elsewhere.status_code == 403
    finally:
        other.close()
    unknown = run.http.get(f"/experiments/{uuid4()}/probe",
                           headers={"X-Bazaar-Account": str(run.account_id)})  # fmt: skip
    assert unknown.status_code == 404
    ok = run.http.get(url, headers={"X-Bazaar-Account": str(run.account_id)}).json()
    assert ok == {"account_id": str(run.account_id), "data_version": "test-v1",
                  "agent_id": str(run.agent_id), "cutoff_at": iso(run.now)}  # fmt: skip


async def test_an_order_placed_while_paging_is_not_skipped(run):
    placed = [run.order("buy", "1") for _ in range(3)]
    assert [p["order_id"] for p in placed] == sorted(p["order_id"] for p in placed)
    tools, client = run.tools()
    async with client:
        first = await tools.orders(window(run, limit=2))
        late = run.order("buy", "1")  # same cutoff, after the cursor was issued
        rest = await tools.orders(window(run, limit=2, cursor=first.data.next_cursor))
    assert first.error is None and rest.error is None
    returned = [str(item.order_id) for item in (*first.data.items, *rest.data.items)]
    assert returned == [*(p["order_id"] for p in placed), late["order_id"]]

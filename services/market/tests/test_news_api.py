"""The news route, read through Duncan's ResearchTools client against the real market app."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from bazaar_agent.research import ResearchContext, ResearchTools
from bazaar_market.app import create_app
from bazaar_market.prices import Bar, close_at, import_bars
from bazaar_market.sources.news_import import import_news_snapshot
from bazaar_market.sources.snapshot import Snapshot
from bazaar_protocol import ExperimentContext, PriceHistoryRequest
from bazaar_protocol.research import ResearchRequest
from fastapi.testclient import TestClient

TOKEN = "runner-secret"
D1, D2, D3 = date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)


def at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


ARTICLES = [
    {"id": 1, "created_at": at(D1, 13), "updated_at": at(D1, 13)},
    {"id": 2, "created_at": at(D1, 15), "updated_at": at(D1, 15)},
    {"id": 3, "created_at": at(D2, 13), "updated_at": at(D2, 13)},
    # Published on D2 but revised on D3: only the revised text is on file.
    {"id": 4, "created_at": at(D2, 14), "updated_at": at(D3, 12)},
]


class Approvals:
    def __init__(self) -> None:
        self.bound: dict[UUID, UUID] = {}

    def grant(self, experiment_id: UUID) -> UUID:
        approval_id = uuid4()
        self.bound[approval_id] = experiment_id
        return approval_id

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return self.bound.get(approval_id) == experiment_id


class Market:
    """A market with bars and news, one experiment on `data_version`, cut off at D2's close."""

    def __init__(self, tmp_path, data_version: str = "demo-bundle-v1", news: bool = True) -> None:
        path = tmp_path / "market.sqlite3"
        snap = Snapshot(tmp_path / "raw", source="alpaca-news", version="v1")
        snap.cover("AAPL", start="2026-01-01T00:00:00Z", end="2026-02-28T23:59:59Z")
        page = {
            "news": [
                {
                    "id": a["id"],
                    "headline": f"headline {a['id']}",
                    "content": f"<p>body {a['id']}</p>",
                    "summary": "",
                    "symbols": ["AAPL"],
                    "created_at": z(a["created_at"]),
                    "updated_at": z(a["updated_at"]),
                }
                for a in ARTICLES
            ],
            "next_page_token": None,
        }
        snap.write("AAPL/page-0001.json", json.dumps(page).encode(), url="u", rows=4)
        with closing(sqlite3.connect(path)) as connection:
            price = Decimal("100.00")
            bars = [Bar("AAPL", d, price, price, price, price, 1) for d in (D1, D2, D3)]
            import_bars(connection, bars, data_version="alpaca-bars-v1", source="synthetic")
            if news:
                import_news_snapshot(connection, snap.dir)
        self.approvals = Approvals()
        self.app = create_app(path, self.approvals, runner_token=TOKEN)
        self.http = TestClient(self.app)
        self.http.__enter__()
        self.experiment_id = uuid4()
        self.approval = self.approvals.grant(self.experiment_id)
        self.agent_id, self.strategy_version_id = uuid4(), uuid4()
        self.data_version = data_version
        self.now = close_at(D2)
        runner = {"X-Bazaar-Approval": str(self.approval), "X-Bazaar-Runner-Token": TOKEN}
        launched = self.http.put(
            f"/experiments/{self.experiment_id}/cutoff",
            headers=runner,
            json={
                "cutoff": z(self.now),
                "data_version": data_version,
                "execution_rule_version": "exec-v1",
            },
        )
        assert launched.status_code == 200, launched.text
        created = self.http.post(
            f"/experiments/{self.experiment_id}/accounts",
            headers=runner,
            json={
                "request_id": str(uuid4()),
                "agent_id": str(self.agent_id),
                "strategy_version_id": str(self.strategy_version_id),
                "cash": "1000.00",
            },
        )
        self.account_id = UUID(created.json()["account_id"])

    def headers(self, account: UUID | None = None) -> dict[str, str]:
        return {
            "X-Bazaar-Approval": str(self.approval),
            "X-Bazaar-Account": str(account or self.account_id),
        }

    def tools(self) -> tuple[ResearchTools, httpx.AsyncClient]:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://market",
            headers=self.headers(),
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

    def url(self, symbol: str = "AAPL") -> str:
        return f"/experiments/{self.experiment_id}/news/{symbol}"

    def close(self) -> None:
        self.http.__exit__(None, None, None)


@pytest.fixture
def market(tmp_path):
    market = Market(tmp_path)
    yield market
    market.close()


def request(market: Market, start: datetime, **fields) -> ResearchRequest:
    return ResearchRequest(symbol="AAPL", start_at=start, end_at=market.now, **fields)


async def test_news_reaches_the_agent_through_its_client(market):
    tools, client = market.tools()
    async with client:
        result = await tools.news(request(market, at(D1, 0)))

    assert result.error is None
    page = result.data
    assert (page.data_version, page.source, page.coverage) == (
        "demo-bundle-v1",
        "alpaca-news/alpaca-news-v1",
        "complete",
    )
    assert [n.record_id for n in page.items] == ["1", "2", "3"]
    assert page.items[0].text == "body 1"
    assert all(n.data_version == "demo-bundle-v1" for n in page.items)


async def test_an_article_revised_after_the_cutoff_is_not_served(market):
    tools, client = market.tools()
    async with client:
        result = await tools.news(request(market, at(D2, 0)))

    assert [n.record_id for n in result.data.items] == ["3"]


async def test_the_client_pages_through_news_with_cursors(market):
    tools, client = market.tools()
    async with client:
        first = await tools.news(request(market, at(D1, 0), limit=2))
        second = await tools.news(
            request(market, at(D1, 0), limit=2, cursor=first.data.next_cursor)
        )

    assert first.error is None and second.error is None
    assert [n.record_id for n in first.data.items] == ["1", "2"]
    assert [n.record_id for n in second.data.items] == ["3"]
    assert second.data.next_cursor is None


async def test_a_window_the_archive_does_not_cover_is_missing_data(market):
    tools, client = market.tools()
    async with client:
        result = await tools.news(request(market, at(date(2025, 12, 1), 0)))

    assert (result.data, result.error.code) == (None, "missing_data")


def test_an_end_after_the_cutoff_is_forbidden(market):
    params = {"start_at": z(at(D1, 0)), "end_at": z(market.now + timedelta(seconds=1))}

    response = market.http.get(market.url(), params=params, headers=market.headers())

    assert (response.status_code, response.json()["error"]["code"]) == (403, "forbidden")


def test_news_needs_an_account_of_this_experiment(market):
    params = {"start_at": z(at(D1, 0)), "end_at": z(market.now)}
    no_account = {"X-Bazaar-Approval": str(market.approval)}

    assert market.http.get(market.url(), params=params, headers=no_account).status_code == 401
    foreign = market.http.get(market.url(), params=params, headers=market.headers(uuid4()))
    assert foreign.status_code == 403


def test_a_symbol_with_no_news_imported_is_missing_data(market):
    params = {"start_at": z(at(D1, 0)), "end_at": z(market.now)}

    response = market.http.get(market.url("MSFT"), params=params, headers=market.headers())

    assert (response.status_code, response.json()["error"]["code"]) == (404, "data_unavailable")


def test_a_plain_bars_experiment_has_no_news(tmp_path):
    market = Market(tmp_path, data_version="alpaca-bars-v1")
    try:
        params = {"start_at": z(at(D1, 0)), "end_at": z(market.now)}
        response = market.http.get(market.url(), params=params, headers=market.headers())
        assert response.status_code == 404
    finally:
        market.close()


async def test_a_bundle_whose_news_was_never_imported_is_missing_data(tmp_path):
    market = Market(tmp_path, news=False)
    try:
        params = {"start_at": z(at(D1, 0)), "end_at": z(market.now)}
        response = market.http.get(market.url(), params=params, headers=market.headers())
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "data_unavailable",
        )
        tools, client = market.tools()
        async with client:
            result = await tools.news(request(market, at(D1, 0)))
        assert (result.data, result.error.code) == (None, "missing_data")
    finally:
        market.close()


async def test_the_client_accepts_bundle_price_pages(market):
    tools, client = market.tools()
    async with client:
        result = await tools.prices(
            PriceHistoryRequest(symbol="AAPL", start_at=close_at(D1), end_at=market.now)
        )

    assert result.error is None
    assert (result.data.data_version, result.data.source) == (
        "demo-bundle-v1",
        "synthetic/alpaca-bars-v1",
    )
    assert [o.observed_at for o in result.data.observations] == [close_at(D1), close_at(D2)]

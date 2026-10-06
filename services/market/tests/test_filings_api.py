"""The filings route, read through Duncan's ResearchTools client against the real market app."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from bazaar_agent.research import FiscalCycle, ResearchContext, ResearchTools
from bazaar_market.sources.filings_import import import_filings_snapshot
from bazaar_protocol import ExperimentContext
from bazaar_protocol.research import ResearchRequest

from .test_filings import CIK, UNPROCESSED, WINDOW, edgar_snapshot
from .test_news_api import Market, z


@pytest.fixture
def market(tmp_path):
    market = Market(tmp_path)
    with closing(sqlite3.connect(market.app.state.market_db_path)) as connection:
        import_filings_snapshot(connection, edgar_snapshot(tmp_path), {"ACME": CIK}, WINDOW)
    yield market
    market.close()


def tools(market: Market, cycle_start: date = date(2025, 1, 1)):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=market.app),
        base_url="http://market",
        headers=market.headers(),
    )
    context = ExperimentContext(
        experiment_id=market.experiment_id,
        agent_id=market.agent_id,
        account_id=market.account_id,
        strategy_version_id=market.strategy_version_id,
        approval_id=market.approval,
        simulated_at=market.now,
        event_sequence=0,
        data_version=market.data_version,
        execution_rule_version="exec-v1",
    )
    cycles = (FiscalCycle(symbol="ACME", start=cycle_start),)
    return ResearchTools(client, ResearchContext(experiment=context, cycles=cycles)), client


def request(market: Market, start: datetime) -> ResearchRequest:
    return ResearchRequest(symbol="ACME", start_at=start, end_at=market.now)


async def test_filings_reach_the_agent_through_its_client(market):
    research, client = tools(market)
    async with client:
        result = await research.filings(request(market, datetime(2024, 7, 1, tzinfo=UTC)))

    assert result.error is None
    page = result.data
    assert (page.data_version, page.source) == ("demo-bundle-v1", "sec-edgar/edgar-filings-v1")
    [item] = page.items
    assert (item.record_id, item.revision, item.form) == ("k-1", "k-1", "10-K")
    assert (item.fiscal_period_start, item.fiscal_period_end) == (
        date(2024, 1, 1),
        date(2024, 12, 31),
    )
    assert item.published_at == item.revised_at == item.available_at


async def test_a_window_before_the_documents_is_missing_data(market):
    research, client = tools(market)
    async with client:
        result = await research.filings(request(market, datetime(2024, 6, 1, tzinfo=UTC)))

    assert (result.data, result.error.code) == (None, "missing_data")


async def test_the_client_refuses_a_filing_from_the_current_cycle(market):
    # The trusted cycle started before the filing's period ended, so serving it would be a leak
    # by the client's rule. The route serves it; the client must reject the page.
    research, client = tools(market, cycle_start=date(2024, 6, 1))
    async with client:
        result = await research.filings(request(market, datetime(2024, 7, 1, tzinfo=UTC)))

    assert (result.data, result.error.code) == (None, "invalid_response")


def test_an_end_after_the_cutoff_is_forbidden(market):
    params = {"start_at": "2024-07-01T00:00:00Z", "end_at": z(market.now + timedelta(seconds=1))}

    response = market.http.get(
        f"/experiments/{market.experiment_id}/filings/ACME", params=params, headers=market.headers()
    )

    assert (response.status_code, response.json()["error"]["code"]) == (403, "forbidden")


def test_a_bundle_with_no_filings_imported_is_missing(tmp_path):
    market = Market(tmp_path)
    try:
        params = {"start_at": "2024-07-01T00:00:00Z", "end_at": z(market.now)}
        response = market.http.get(
            f"/experiments/{market.experiment_id}/filings/ACME",
            params=params,
            headers=market.headers(),
        )
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "data_unavailable",
        )
    finally:
        market.close()


async def test_a_window_holding_an_excluded_filing_reaches_the_client_as_missing(tmp_path):
    market = Market(tmp_path)
    try:
        with closing(sqlite3.connect(market.app.state.market_db_path)) as connection:
            snapshot = edgar_snapshot(tmp_path, extra=[UNPROCESSED])
            import_filings_snapshot(connection, snapshot, {"ACME": CIK}, WINDOW)
        research, client = tools(market)
        async with client:
            result = await research.filings(request(market, datetime(2024, 7, 1, tzinfo=UTC)))
        assert (result.data, result.error.code) == (None, "missing_data")
    finally:
        market.close()


def test_a_database_without_the_exclusions_table_answers_404_not_500(market):
    with closing(sqlite3.connect(market.app.state.market_db_path)) as connection:
        connection.execute("DROP TABLE data_filings_exclusions")
    params = {"start_at": "2024-07-01T00:00:00Z", "end_at": z(market.now)}

    response = market.http.get(
        f"/experiments/{market.experiment_id}/filings/ACME", params=params, headers=market.headers()
    )

    assert (response.status_code, response.json()["error"]["code"]) == (404, "data_unavailable")

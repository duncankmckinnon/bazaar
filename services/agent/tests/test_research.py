"""Synthetic archive/adapter fixtures only; no live market, model or telemetry token."""

import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from bazaar_agent.research import FiscalCycle, ResearchContext, ResearchTools
from bazaar_protocol import ExperimentContext, OrderRequest
from bazaar_protocol.research import HistoryRequest, PrivateHistoryPage, ResearchRequest
from pydantic import ValidationError

NOW = "2020-04-01T12:00:00Z"
EARLY = "2020-03-01T00:00:00Z"
FUTURE = "2020-04-01T12:00:01Z"
SECRET = "SECRET-FUTURE-TEXT-TOKEN"
IDS = {
    name: str(UUID(int=i))
    for i, name in enumerate(
        ("experiment_id", "account_id", "agent_id", "strategy_version_id", "approval_id"), 1
    )
}


def context():
    return ResearchContext(
        experiment=ExperimentContext(
            **IDS,
            simulated_at=NOW,
            event_sequence=10,
            data_version="v1",
            execution_rule_version="immediate-v1",
        ),
        cycles=(FiscalCycle(symbol="AAPL", start="2020-01-01"),),
    )


def request(**updates):
    return ResearchRequest(symbol="AAPL", start_at=EARLY, end_at=NOW, **updates)


def history_request(**updates):
    return HistoryRequest(start_at=EARLY, end_at=NOW, **updates)


def account(**updates):
    return {
        **{k: v for k, v in IDS.items() if k != "approval_id"},
        "simulated_at": NOW,
        "state_version": 1,
        "cash": "100",
        "holdings": [],
        **updates,
    }


def portfolio(**updates):
    return {
        "account_id": IDS["account_id"],
        "experiment_id": IDS["experiment_id"],
        "simulated_at": NOW,
        "state_version": 1,
        "cash": "100",
        "holdings": [],
        "portfolio_value": "100",
        "source": "archive",
        "data_version": "v1",
        "valuation_rule_version": "mark-v1",
        **updates,
    }


def news(**updates):
    return {
        "source": "archive",
        "data_version": "v1",
        "record_id": "n1",
        "revision": "r1",
        "published_at": NOW,
        "available_at": NOW,
        "revised_at": NOW,
        "symbol": "AAPL",
        "headline": "Historical headline",
        "text": SECRET,
        **updates,
    }


def filing(**updates):
    result = news()
    result.pop("headline")
    return {
        **result,
        "fiscal_period_start": "2019-10-01",
        "fiscal_period_end": "2019-12-31",
        "form": "10-Q/A",
        **updates,
    }


def private_record(**updates):
    result = news()
    result.pop("symbol")
    result.pop("headline")
    return {**result, "kind": "cache", "simulated_at": NOW, "event_sequence": 10, **updates}


def page(items=(), **updates):
    return {
        **{k: v for k, v in IDS.items() if k != "approval_id"},
        "cutoff_at": NOW,
        "start_at": EARLY,
        "end_at": NOW,
        "source": "archive",
        "data_version": "v1",
        "coverage": "complete",
        "items": list(items),
        **updates,
    }


def prices(**updates):
    return {
        "experiment_id": IDS["experiment_id"],
        "symbol": "AAPL",
        "cutoff_at": NOW,
        "source": "archive",
        "data_version": "v1",
        "observations": [{"observed_at": NOW, "available_at": NOW, "price": "12.34"}],
        **updates,
    }


def order_request():
    return OrderRequest(client_order_id=UUID(int=99), symbol="AAPL", side="buy", quantity="1")


def order(status="filled", **updates):
    result = {
        **order_request().model_dump(mode="json"),
        "status": status,
        "order_id": str(UUID(int=100)),
        "account": account(),
    }
    if status == "filled":
        result.update(
            unit_price="12.34",
            fee="0",
            executed_at=NOW,
            price_observed_at=NOW,
            price_available_at=NOW,
            price_source="archive",
            data_version="v1",
            execution_rule_version="immediate-v1",
        )
    else:
        result.update(rejected_at=NOW, error={"code": "insufficient_cash", "message": SECRET})
    return {**result, **updates}


async def invoke(payload, method="news", req=None, status=200, ctx=None):
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(
        base_url="https://market.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        tools = ResearchTools(client, ctx or context())
        fn = getattr(tools, method)
        result = await fn() if req is None else await fn(req)
    return result, calls


@pytest.mark.parametrize(
    "method,payload,req,suffix",
    [
        ("account", account(), None, f"/accounts/{IDS['account_id']}"),
        ("portfolio", portfolio(), None, f"/accounts/{IDS['account_id']}/portfolio"),
        ("prices", prices(), request(), "/prices/AAPL"),
        ("news", page([news()]), request(), "/news/AAPL"),
        ("filings", page([filing()]), request(), "/filings/AAPL"),
        (
            "account_history",
            page([account()]),
            history_request(),
            f"/accounts/{IDS['account_id']}/history",
        ),
        (
            "portfolio_history",
            page([portfolio()]),
            history_request(),
            f"/accounts/{IDS['account_id']}/portfolio/history",
        ),
        ("orders", page([order()]), history_request(), f"/accounts/{IDS['account_id']}/orders"),
    ],
)
async def test_typed_routes(method, payload, req, suffix):
    result, calls = await invoke(payload, method, req)
    assert result.error is None
    assert result.data is not None
    assert calls[0].url.path == f"/experiments/{IDS['experiment_id']}{suffix}"
    assert calls[0].method == "GET"
    if req:
        assert calls[0].url.params["end_at"] == NOW
        assert "symbol" not in calls[0].url.params


@pytest.mark.parametrize(
    "mutation",
    [
        {"published_at": FUTURE},
        {"available_at": FUTURE},
        {"revised_at": FUTURE},
        {"published_at": "2020-04-01T11:00:00+01:00"},
        {"published_at": 123456},
        {"published_at": "123456"},
        {"symbol": "MSFT"},
        {"data_version": "future-v2"},
        {"source": "live-search"},
        {"published_at": "2020-02-01T00:00:00Z"},
        {"revised_at": "2020-03-01T00:00:00Z"},
        {"future_label": SECRET},
    ],
)
async def test_news_rejects_future_revision_scope_schema_and_window(mutation):
    result, _ = await invoke(page([news(**mutation)]), req=request())
    assert result.data is None
    assert result.error.code == "invalid_response"
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        {"experiment_id": str(UUID(int=20))},
        {"account_id": str(UUID(int=20))},
        {"agent_id": str(UUID(int=20))},
        {"strategy_version_id": str(UUID(int=20))},
        {"cutoff_at": FUTURE},
        {"cutoff_at": EARLY},
        {"data_version": "v2"},
        {"start_at": NOW},
        {"end_at": EARLY},
    ],
)
async def test_page_scope_cutoff_and_window(mutation):
    result, _ = await invoke(page([news()], **mutation), req=request())
    assert result.data is None
    assert result.error.code == "invalid_response"


@pytest.mark.parametrize("coverage", ["missing", "partial"])
async def test_coverage_is_not_empty_success(coverage):
    result, _ = await invoke(page(coverage=coverage), req=request())
    assert result.error.code == "missing_data"
    assert result.data is None


async def test_empty_complete_archive_is_explicit_success():
    result, _ = await invoke(page(), req=request())
    assert result.data.items == ()


@pytest.mark.parametrize(
    "mutation",
    [
        {"fiscal_period_end": "2020-01-01"},
        {"fiscal_period_start": "2021-01-01"},
        {"available_at": FUTURE},
        {"revised_at": FUTURE},
    ],
)
async def test_filing_prior_cycle_release_and_amendment(mutation):
    result, _ = await invoke(page([filing(**mutation)]), "filings", request())
    assert result.error.code == "invalid_response"
    assert result.data is None


async def test_fiscal_calendar_not_guessed():
    ctx = context().model_copy(update={"cycles": ()})
    result, calls = await invoke(page([filing()]), "filings", request(), ctx=ctx)
    assert result.error.code == "unsupported"
    assert not calls


@pytest.mark.parametrize("method,payload", [("account", account()), ("portfolio", portfolio())])
@pytest.mark.parametrize("mutation", [{"simulated_at": FUTURE}, {"account_id": str(UUID(int=30))}])
async def test_snapshot_boundary(method, payload, mutation):
    result, _ = await invoke({**payload, **mutation}, method)
    assert result.error.code == "invalid_response"


@pytest.mark.parametrize(
    "mutation",
    [
        {"observations": [{"observed_at": NOW, "available_at": FUTURE, "price": "1"}]},
        {
            "observations": [
                {"observed_at": "2020-02-01T00:00:00Z", "available_at": NOW, "price": "1"}
            ]
        },
        {"symbol": "MSFT"},
        {"data_version": "v2"},
        {"observations": [{"observed_at": NOW, "available_at": NOW, "price": 0.5}]},
    ],
)
async def test_prices_boundary(mutation):
    result, _ = await invoke(prices(**mutation), "prices", request())
    assert result.error.code == "invalid_response"


async def test_future_request_and_unknown_cursor_never_sent():
    for req in (
        ResearchRequest(symbol="AAPL", start_at=EARLY, end_at=FUTURE),
        request(cursor="stolen"),
    ):
        result, calls = await invoke(page(), req=req)
        assert result.error is not None
        assert not calls


async def test_pagination_bound_to_query_and_context():
    responses = [page([news()], next_cursor="opaque"), page([news(record_id="n2")])]
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(
        base_url="https://market.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        tools = ResearchTools(client, context())
        first = await tools.news(request(limit=1))
        assert first.data.next_cursor == "opaque"
        assert (await tools.news(request(limit=2, cursor="opaque"))).error is not None
        assert (
            await ResearchTools(client, context()).news(request(limit=1, cursor="opaque"))
        ).error is not None
        assert (await tools.news(request(limit=1, cursor="opaque"))).data.items[0].record_id == "n2"
    assert len(calls) == 2
    assert calls[1].url.params["cursor"] == "opaque"


@pytest.mark.parametrize(
    "payload",
    [
        page([news(), news()]),
        page([news(record_id="z"), news(record_id="a")]),
        page(next_cursor="empty"),
        page([news(), news(record_id="n2")]),
    ],
)
async def test_pagination_duplicates_order_and_size(payload):
    result, _ = await invoke(payload, req=request(limit=1))
    assert result.error.code == "invalid_response"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "missing_data"),
        (409, "conflict"),
        (422, "invalid_request"),
        (500, "server_error"),
        (501, "unsupported"),
        (302, "server_error"),
    ],
)
async def test_safe_http_errors(status, code):
    result, calls = await invoke(
        {"message": SECRET, "credentials": SECRET}, req=request(), status=status
    )
    assert result.error.code == code
    assert SECRET not in result.model_dump_json()
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["filled", "rejected"])
async def test_orders_structured_and_idempotent(status):
    bodies = []

    def handler(r):
        bodies.append(json.loads(r.content))
        return httpx.Response(200, json=order(status))

    async with httpx.AsyncClient(
        base_url="https://market.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        tools = ResearchTools(client, context())
        one = await tools.place_order(order_request())
        two = await tools.place_order(order_request())
    assert one.error is None and one == two
    assert bodies == [order_request().model_dump(mode="json")] * 2
    assert SECRET not in one.model_dump_json()


@pytest.mark.parametrize(
    "mutation",
    [
        {"client_order_id": str(UUID(int=80))},
        {"symbol": "MSFT"},
        {"quantity": "2"},
        {"data_version": "v2"},
        {"execution_rule_version": "future"},
        {"executed_at": FUTURE, "account": account(simulated_at=FUTURE)},
        {"account": account(account_id=str(UUID(int=70)))},
    ],
)
async def test_order_response_matches_request_and_scope(mutation):
    result, _ = await invoke(order(**mutation), "place_order", order_request())
    assert result.data is None
    assert result.error.code == "invalid_response"


async def test_ambiguous_order_network_failure_no_retry_or_secret(capfire):
    calls = []

    def handler(r):
        calls.append(r)
        raise httpx.ReadTimeout(SECRET, request=r)

    async with httpx.AsyncClient(
        base_url="https://market.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ResearchTools(client, context()).place_order(order_request())
    assert len(calls) == 1
    assert result.error.code == "network"
    assert "same ID" in result.error.message
    assert SECRET not in result.model_dump_json()
    assert SECRET not in json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)


class FakePrivateReader:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def read(self, ctx, req):
        self.calls.append((ctx, req))
        if isinstance(self.payload, Exception):
            raise self.payload
        return PrivateHistoryPage.model_validate(self.payload)


async def test_private_fail_closed_default():
    result, _ = await invoke({}, "private_history", history_request())
    assert result.error.code == "unsupported"


@pytest.mark.parametrize(
    "mutation",
    [
        {"simulated_at": FUTURE},
        {"event_sequence": 11},
        {"available_at": FUTURE},
        {"data_version": "v2"},
        {"published_at": FUTURE},
        {"revised_at": FUTURE},
    ],
)
async def test_private_notes_cache_attempts_boundary(mutation):
    adapter = FakePrivateReader(page([private_record(**mutation)]))
    async with httpx.AsyncClient(base_url="https://market.invalid") as client:
        result = await ResearchTools(client, context(), adapter).private_history(history_request())
    assert result.error.code == "invalid_response"
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize("kind", ["note", "cache", "trace", "attempt"])
async def test_private_scoped_adapter_same_time_edge(kind):
    adapter = FakePrivateReader(page([private_record(kind=kind)]))
    async with httpx.AsyncClient(base_url="https://market.invalid") as client:
        result = await ResearchTools(client, context(), adapter).private_history(history_request())
    assert result.error is None
    assert adapter.calls == [(context().experiment, history_request())]


@pytest.mark.parametrize("payload", [page(account_id=str(UUID(int=60))), RuntimeError(SECRET)])
async def test_private_malicious_scope_and_errors(payload, capfire):
    async with httpx.AsyncClient(base_url="https://market.invalid") as client:
        result = await ResearchTools(client, context(), FakePrivateReader(payload)).private_history(
            history_request()
        )
    assert result.error.code == "invalid_response"
    assert SECRET not in result.model_dump_json()
    assert SECRET not in json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)


async def test_payloads_not_in_spans(capfire):
    result, _ = await invoke(page([news()]), req=request())
    assert result.data.items[0].text == SECRET
    invalid, _ = await invoke(page([news(future_labels=SECRET)]), req=request())
    assert invalid.error is not None
    spans = capfire.exporter.exported_spans_as_dict()
    assert "news tool" in {s["name"] for s in spans}
    assert SECRET not in json.dumps(spans, default=str)


def test_no_unrestricted_history_query_or_credentials():
    with pytest.raises(ValidationError):
        HistoryRequest(start_at=EARLY, end_at=NOW, query="SELECT *", api_key=SECRET)
    with pytest.raises(ValidationError):
        ResearchRequest(symbol="../../evil", start_at=EARLY, end_at=NOW)
    assert context().experiment.simulated_at == datetime(2020, 4, 1, 12, tzinfo=UTC)


async def test_rejected_order_history_messages_sanitized():
    result, _ = await invoke(page([order("rejected")]), "orders", history_request())
    assert result.error is None
    assert result.data.items[0].error.message == "insufficient_cash"
    assert SECRET not in result.model_dump_json()


async def test_cross_page_duplicate_rejected():
    async with httpx.AsyncClient(
        base_url="https://market.invalid",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=page([news()], next_cursor="next"))
        ),
    ) as client:
        tools = ResearchTools(client, context())
        assert (await tools.news(request())).error is None
        assert (await tools.news(request(cursor="next"))).error.code == "invalid_response"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError])
async def test_client_network_errors_sanitized(error_type):
    def handler(r):
        raise error_type(SECRET, request=r)

    async with httpx.AsyncClient(
        base_url="https://market.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        result = await ResearchTools(client, context()).news(request())
    assert result.error.code == "network"
    assert SECRET not in result.model_dump_json()


async def test_non_json_error_and_success_payloads_safe(capfire):
    for status in (200, 500):
        async with httpx.AsyncClient(
            base_url="https://market.invalid",
            transport=httpx.MockTransport(
                lambda r, status=status: httpx.Response(status, text=SECRET)
            ),
        ) as client:
            result = await ResearchTools(client, context()).account()
        assert result.error is not None
        assert SECRET not in result.model_dump_json()
    assert SECRET not in json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)


async def test_delayed_revision_available_exactly_at_cutoff():
    item = news(published_at=EARLY, revised_at="2020-03-31T00:00:00Z", available_at=NOW)
    result, _ = await invoke(page([item]), req=request())
    assert result.error is None
    assert result.data.items[0].available_at == context().experiment.simulated_at


async def test_private_missing_coverage_and_contaminated_inherited_state():
    for payload, code in (
        (page(coverage="partial"), "missing_data"),
        (page([private_record(kind="attempt", simulated_at=FUTURE)]), "invalid_response"),
        (page(strategy_version_id=str(UUID(int=101))), "invalid_response"),
    ):
        async with httpx.AsyncClient(base_url="https://market.invalid") as client:
            result = await ResearchTools(
                client, context(), FakePrivateReader(payload)
            ).private_history(history_request())
        assert result.error.code == code
        assert result.data is None


async def test_bypassed_request_validation_revalidated_before_http():
    malformed = request().model_copy(update={"symbol": "../../evil"})
    result, calls = await invoke(page(), req=malformed)
    assert result.error is not None
    assert not calls

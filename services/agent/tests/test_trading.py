"""Local fixture models only: no API keys, real historical runs or model calls."""

import asyncio
import json
from uuid import UUID

import httpx
import logfire
import pytest
from bazaar_agent.trading import DecisionBudget, run_decision
from bazaar_protocol.registry import AgentRecord, StrategyDefinition, StrategyVersion
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from .test_research import (
    EARLY,
    FUTURE,
    IDS,
    NOW,
    SECRET,
    account,
    context,
    filing,
    news,
    order,
    order_request,
    page,
    portfolio,
    prices,
    private_record,
)

ORDER_ID = UUID(int=99)


def inputs(**definition):
    identity = AgentRecord(
        agent_id=IDS["agent_id"],
        name="fixture-trader",
        strategy_id=UUID(int=50),
        created_at=NOW,
        created_by="fixture",
    )
    version = StrategyVersion(
        version_id=IDS["strategy_version_id"],
        strategy_id=identity.strategy_id,
        version=1,
        definition=StrategyDefinition(model_ref="fixture", instructions=SECRET, **definition),
        parent_version_id=None,
        hypothesis="fixture",
        definition_digest="a" * 64,
        created_at=NOW,
        created_by="fixture",
    )
    return {
        "identity": identity,
        "version": version,
        "context": context(),
        "client_order_id": ORDER_ID,
    }


def query(**updates):
    return {"symbol": "AAPL", "start_at": EARLY, "end_at": NOW, **updates}


def history(**updates):
    return {"start_at": EARLY, "end_at": NOW, **updates}


def output(info, action="hold"):
    return ToolCallPart(info.output_tools[0].name, {"action": action})


def script(*steps):
    calls = []

    def model(messages, info):
        calls.append((messages, info))
        step = steps[len(calls) - 1]
        return ModelResponse(step(info) if callable(step) else step)

    return FunctionModel(model), calls


async def invoke(model=None, *, payload=None, handler=None, overrides=None, **definition):
    requests = []

    def transport(request):
        requests.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json=payload or account())

    async with httpx.AsyncClient(
        base_url="https://market.invalid", transport=httpx.MockTransport(transport), timeout=2
    ) as client:
        args = inputs(**definition)
        args.update(overrides or {})
        if model is not None:
            args["model_factory"] = lambda ref: model
        result = await run_decision(client=client, **args)
    return result, requests


async def test_testmodel_hold_and_missing_factory():
    result, calls = await invoke(TestModel(call_tools=[], custom_output_args={"action": "hold"}))
    assert result.decision.action == "hold" and result.error is None
    assert result.usage.model_requests == 1 and not calls
    result, calls = await invoke()
    assert result.error.code == "unsupported" and not calls


@pytest.mark.parametrize(
    "definition",
    [
        {"harness": "monty"},
        {"harness": "orchestrated"},
        {"tools": ("monty",)},
        {"artifact_ref": "https://private.invalid/code.py"},
    ],
)
async def test_unsupported_capabilities_fail_before_factory(definition):
    seen = []
    result, calls = await invoke(
        overrides={"model_factory": lambda ref: seen.append(ref)}, **definition
    )
    assert result.error.code == "unsupported" and not seen and not calls


@pytest.mark.parametrize("field", ["agent_id", "strategy_version_id"])
async def test_context_identity_mismatch(field):
    ctx = context().model_copy(
        update={"experiment": context().experiment.model_copy(update={field: UUID(int=900)})}
    )
    result, calls = await invoke(overrides={"context": ctx})
    assert result.error.code == "invalid_request" and not calls


async def test_strategy_identity_mismatch_and_revalidate_inputs():
    args = inputs()
    args["identity"] = args["identity"].model_copy(update={"strategy_id": UUID(int=999)})
    result, _ = await invoke(overrides=args)
    assert result.error.code == "invalid_request"
    bad = DecisionBudget().model_copy(update={"tool_calls": -1})
    result, _ = await invoke(overrides={"budget": bad})
    assert result.error.code == "invalid_request"


@pytest.mark.parametrize(
    "capability,names",
    [
        ("account", {"account", "portfolio", "account_history", "portfolio_history"}),
        ("market_history", {"prices"}),
        ("news", {"news"}),
        ("reports", {"filings"}),
        ("private_history", {"private_history"}),
        ("orders", {"orders", "market_order"}),
    ],
)
async def test_only_defined_tools_visible(capability, names):
    model, calls = script(lambda info: [output(info)])
    result, _ = await invoke(model, tools=(capability,))
    assert result.error is None
    assert {tool.name for tool in calls[0][1].function_tools} == names
    assert calls[0][1].instructions.endswith(SECRET)


@pytest.mark.parametrize(
    "method,capability,args,payload",
    [
        ("account", "account", {}, account()),
        ("portfolio", "account", {}, portfolio()),
        ("prices", "market_history", query(), prices()),
        ("news", "news", query(), page([news()])),
        ("filings", "reports", query(), page([filing()])),
        ("account_history", "account", history(), page([account()])),
        ("portfolio_history", "account", history(), page([portfolio()])),
        ("orders", "orders", history(), page([order()])),
    ],
)
async def test_typed_research_wrappers(method, capability, args, payload):
    model, calls = script([ToolCallPart(method, args)], lambda info: [output(info)])
    result, requests = await invoke(model, payload=payload, tools=(capability,), harness="research")
    assert result.error is None and result.decision.action == "hold"
    assert result.usage.tool_calls == 1 and len(requests) == 1
    assert str(context().experiment.experiment_id) in requests[0].url.path
    if method == "news":
        assert SECRET in str(calls[1][0])


async def test_private_reader_scope_and_untrusted_text():
    from bazaar_protocol.research import PrivateHistoryPage

    class Reader:
        async def read(self, ctx, request):
            assert ctx == context().experiment
            return PrivateHistoryPage.model_validate(page([private_record()]))

    model, calls = script([ToolCallPart("private_history", history())], lambda info: [output(info)])
    result, requests = await invoke(
        model, tools=("private_history",), overrides={"private_history": Reader()}
    )
    assert result.error is None and not requests
    assert SECRET in str(calls[1][0])


@pytest.mark.parametrize(
    "side,status", [("buy", "filled"), ("sell", "filled"), ("buy", "rejected")]
)
async def test_structured_orders_and_terminal_rejections(side, status):
    request = {**order_request().model_dump(mode="json"), "side": side}
    model, _ = script(
        [ToolCallPart("market_order", request)], lambda info: [output(info, "ordered")]
    )
    result, calls = await invoke(model, payload=order(status=status, side=side), tools=("orders",))
    assert result.error is None and result.order_result.status == status
    assert result.decision.action == "ordered" and len(calls) == 1
    assert json.loads(calls[0].content) == request


async def test_repeated_order_returns_evidence_without_retransmission():
    step = [ToolCallPart("market_order", order_request().model_dump(mode="json"))]
    model, _ = script(step, step, lambda info: [output(info, "ordered")])
    result, calls = await invoke(model, payload=order(), tools=("orders",))
    assert result.error is None and len(calls) == 1 and result.usage.tool_calls == 2


@pytest.mark.parametrize("change", [{"client_order_id": str(UUID(int=101))}, {"quantity": "2"}])
async def test_no_fresh_ids_or_changed_order_after_submission(change):
    original = order_request().model_dump(mode="json")
    model, _ = script(
        [ToolCallPart("market_order", original)],
        [ToolCallPart("market_order", {**original, **change})],
    )
    result, calls = await invoke(model, payload=order(), tools=("orders",))
    assert result.error.code in ("conflict", "invalid_request") and len(calls) == 1
    assert result.order_request == order_request() and result.order_result.status == "filled"
    assert result.decision is None


async def test_wrong_initial_id_never_posts():
    request = {**order_request().model_dump(mode="json"), "client_order_id": str(UUID(int=2))}
    model, _ = script([ToolCallPart("market_order", request)])
    result, calls = await invoke(model, tools=("orders",))
    assert result.error.code == "invalid_request" and not calls and result.order_request is None


@pytest.mark.parametrize("failure", ["network", "malformed", "scope", "server", "hook"])
async def test_ambiguous_order_aborts_without_model_retry(failure):
    def handler(request):
        if failure == "network":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "hook":
            raise RuntimeError(SECRET)
        if failure == "malformed":
            return httpx.Response(200, content=SECRET)
        if failure == "server":
            return httpx.Response(500, text=SECRET)
        return httpx.Response(200, json=order(account=account(account_id=str(UUID(int=77)))))

    model, model_calls = script(
        [ToolCallPart("market_order", order_request().model_dump(mode="json"))]
    )
    result, calls = await invoke(model, handler=handler, tools=("orders",))
    assert result.error is not None and result.decision is None
    assert result.order_request == order_request() and result.order_result is None
    assert len(calls) == len(model_calls) == 1
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(
    "query_args,payload,code",
    [
        (query(end_at=FUTURE), page(), "invalid_request"),
        (query(cursor="unknown"), page(), "invalid_request"),
        (query(), page(coverage="partial"), "missing_data"),
        (query(), page([news(available_at=FUTURE)]), "invalid_response"),
    ],
)
async def test_scoped_failure_never_advances_to_another_model_call(query_args, payload, code):
    model, calls = script([ToolCallPart("news", query_args)])
    result, _ = await invoke(model, payload=payload, tools=("news",))
    assert result.error.code == code and len(calls) == 1 and result.decision is None


async def test_cursor_issued_this_run_only_and_no_automatic_page_walking():
    model, _ = script(
        [ToolCallPart("news", query())],
        [ToolCallPart("news", query(cursor="next"))],
        lambda info: [output(info)],
    )

    def handler(request):
        return httpx.Response(
            200,
            json=page() if "cursor" in request.url.params else page([news()], next_cursor="next"),
        )

    result, calls = await invoke(model, handler=handler, tools=("news",))
    assert result.error is None and len(calls) == 2
    second, _ = script([ToolCallPart("news", query(cursor="next"))])
    result, calls = await invoke(second, handler=handler, tools=("news",))
    assert result.error.code == "invalid_request" and not calls


@pytest.mark.parametrize(
    "budget,steps,expected_requests",
    [
        (DecisionBudget(model_requests=1), [[ToolCallPart("account", {})]], 1),
        (DecisionBudget(tool_calls=0), [[ToolCallPart("account", {})]], 1),
        (
            DecisionBudget(tool_calls=1),
            [[ToolCallPart("account", {}), ToolCallPart("portfolio", {})]],
            1,
        ),
        (DecisionBudget(total_tokens=1), [lambda info: [output(info)]], 1),
    ],
)
async def test_model_tool_and_token_budgets(budget, steps, expected_requests):
    model, calls = script(*steps)
    result, requests = await invoke(model, overrides={"budget": budget})
    assert result.error.code == "conflict" and result.decision is None
    assert len(calls) == expected_requests
    if budget.tool_calls < 2:
        assert not requests  # entire batch rejected before side effects


async def test_timeout_and_cancellation():
    async def slow(messages, info):
        await asyncio.sleep(2)
        return ModelResponse([output(info)])

    result, _ = await invoke(
        FunctionModel(slow), overrides={"budget": DecisionBudget(timeout_seconds=0.01)}
    )
    assert result.error.code == "conflict"

    async def cancel(messages, info):
        raise asyncio.CancelledError(SECRET)

    with pytest.raises(asyncio.CancelledError):
        await invoke(FunctionModel(cancel))


@pytest.mark.parametrize(
    "bad", [{"action": "buy"}, {"action": "hold", "sql": SECRET}, {"action": "ordered"}]
)
async def test_invalid_outputs_retry_bounded_without_orders(bad):
    model, calls = script(
        lambda info: [ToolCallPart(info.output_tools[0].name, bad)],
        lambda info: [output(info)],
    )
    result, requests = await invoke(model, tools=())
    assert result.error is None and result.decision.action == "hold"
    assert len(calls) == 2 and not requests


async def test_exhausted_output_retries_preserve_settlement():
    model, _ = script(
        [ToolCallPart("market_order", order_request().model_dump(mode="json"))],
        lambda info: [output(info, "hold")],
        lambda info: [output(info, "hold")],
    )
    result, calls = await invoke(model, payload=order(), tools=("orders",))
    assert result.error.code == "invalid_response" and result.decision is None
    assert result.order_result.status == "filled" and len(calls) == 1


async def test_invalid_tool_arguments_can_retry_without_http():
    invalid = {**order_request().model_dump(mode="json"), "quantity": 1.25, "simulated_at": FUTURE}
    model, _ = script([ToolCallPart("market_order", invalid)], lambda info: [output(info)])
    result, calls = await invoke(model, tools=("orders",))
    assert result.error is None and not calls and result.order_request is None


async def test_unknown_or_unpermitted_tool_never_executes():
    model, _ = script(
        [ToolCallPart("market_order", order_request().model_dump(mode="json"))],
        lambda info: [output(info)],
    )
    result, calls = await invoke(model, tools=())
    assert result.error is None and not calls


async def test_fixture_factory_error_sanitized():
    def factory(ref):
        raise RuntimeError(SECRET)

    result, calls = await invoke(overrides={"model_factory": factory})
    assert (
        result.error.code == "server_error" and SECRET not in result.model_dump_json() and not calls
    )


async def test_payload_safe_with_production_httpx_and_global_genai_instrumentation(capfire, caplog):
    Agent.instrument_all(True)
    try:
        model, _ = script(
            [ToolCallPart("news", query())],
            [ToolCallPart("news", query(cursor=SECRET))],
            lambda info: [output(info)],
        )

        def handler(request):
            if "cursor" in request.url.params:
                raise httpx.ReadTimeout(SECRET, request=request)
            return httpx.Response(200, json=page([news()], next_cursor=SECRET))

        async with httpx.AsyncClient(
            base_url="https://market.invalid",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer " + SECRET},
        ) as client:
            logfire.instrument_httpx(
                client,
                capture_all=False,
                capture_headers=False,
                capture_request_body=False,
                capture_response_body=False,
            )
            result = await run_decision(
                client=client, model_factory=lambda ref: model, **inputs(tools=("news",))
            )
        assert result.error.code == "network"
        spans = capfire.exporter.exported_spans_as_dict()
        assert "trading.decision" in {span["name"] for span in spans}
        assert SECRET not in json.dumps(spans, default=str) + caplog.text
        assert not any("gen_ai" in json.dumps(span, default=str) for span in spans)
    finally:
        Agent.instrument_all(False)


async def test_nonfixture_factory_result_rejected():
    result, calls = await invoke(overrides={"model_factory": lambda ref: object()})
    assert result.error.code == "unsupported" and not calls


async def test_model_budget_after_order_retains_evidence():
    model, _ = script([ToolCallPart("market_order", order_request().model_dump(mode="json"))])
    result, calls = await invoke(
        model,
        payload=order(),
        tools=("orders",),
        overrides={"budget": DecisionBudget(model_requests=1)},
    )
    assert result.error.code == "conflict" and result.decision is None
    assert result.order_result.status == "filled" and len(calls) == 1


async def test_sequential_batch_error_prevents_later_order():
    model, _ = script(
        [
            ToolCallPart("account", {}),
            ToolCallPart("market_order", order_request().model_dump(mode="json")),
        ]
    )
    result, calls = await invoke(model, payload=account(account_id=str(UUID(int=999))))
    assert result.error.code == "invalid_response" and len(calls) == 1
    assert result.order_request is None


@pytest.mark.parametrize("kind", ["filing", "private", "order", "model_failure", "invalid_output"])
async def test_other_protected_payloads_not_in_monitored_spans(kind, capfire, caplog):
    from bazaar_protocol.research import PrivateHistoryPage

    class Reader:
        async def read(self, ctx, request):
            return PrivateHistoryPage.model_validate(page([private_record()]))

    if kind == "filing":
        model, _ = script([ToolCallPart("filings", query())], lambda info: [output(info)])
        payload = page([filing()])
        capability = "reports"
    elif kind == "private":
        model, _ = script([ToolCallPart("private_history", history())], lambda info: [output(info)])
        payload = page()
        capability = "private_history"
    elif kind == "order":
        model, _ = script(
            [ToolCallPart("market_order", order_request().model_dump(mode="json"))],
            lambda info: [output(info, "ordered")],
        )
        payload = order(status="rejected")
        capability = "orders"
    elif kind == "invalid_output":
        model, _ = script(
            lambda info: [ToolCallPart(info.output_tools[0].name, {"action": SECRET})],
            lambda info: [output(info)],
        )
        payload = account()
        capability = "account"
    else:

        def failing(messages, info):
            raise RuntimeError(SECRET)

        model = FunctionModel(failing)
        payload = account()
        capability = "account"
    async with httpx.AsyncClient(
        base_url="https://market.invalid",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        headers={"Authorization": SECRET},
    ) as client:
        logfire.instrument_httpx(
            client,
            capture_all=False,
            capture_headers=False,
            capture_request_body=False,
            capture_response_body=False,
        )
        result = await run_decision(
            client=client,
            model_factory=lambda ref: model,
            private_history=Reader(),
            **inputs(tools=(capability,)),
        )
    assert (result.error is None) == (kind != "model_failure")
    spans = capfire.exporter.exported_spans_as_dict()
    assert SECRET not in json.dumps(spans, default=str) + caplog.text
    attributes = next(span["attributes"] for span in spans if span["name"] == "trading.decision")
    for label in ("experiment_id", "account_id", "agent_id", "strategy_version_id"):
        assert attributes[label] == IDS[label]

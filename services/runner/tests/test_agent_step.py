import asyncio
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import httpx
import pytest
from bazaar_protocol import OrderRequest, OrderSide, order_result_adapter
from bazaar_runner.agent_step import (
    ACCOUNT_HEADER,
    AgentDecision,
    AgentStep,
    reserved_order_id,
)
from bazaar_runner.http_market import (
    APPROVAL_HEADER,
    RUNNER_TOKEN_HEADER,
    HttpMarketPort,
    parse_fiscal_cycles,
    parse_order_page,
)
from bazaar_runner.market import FiscalCycle
from bazaar_runner.record import RunRecord, record_run
from bazaar_runner.run import RunState, run_strategy

from .market_fakes import SESSIONS, SPEC, TOKEN, InMemoryMarket, delegating_transport

MARKET_URL = "http://market"


def agent_step(fake: InMemoryMarket, decide, seen: list[httpx.Request] | None = None) -> AgentStep:
    return AgentStep(decide, market_url=MARKET_URL, transport=delegating_transport(fake, seen))


def buy_order(client_order_id: UUID, quantity: int = 3) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id, symbol="AAPL", side=OrderSide.BUY, quantity=quantity
    )


async def post_order(client: httpx.AsyncClient, ctx, order: OrderRequest):
    """What an agent does: place the order itself, over its own client."""
    path = f"/experiments/{ctx.experiment_id}/accounts/{ctx.account_id}/orders"
    response = await client.post(path, json=order.model_dump(mode="json"))
    response.raise_for_status()
    return order_result_adapter.validate_json(response.content)


def buys_at_first_decision(then=None):
    async def decide(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        order = buy_order(client_order_id)
        result = await post_order(client, ctx, order)
        if then is not None:
            then()
        return AgentDecision(order, result, None)

    return decide


def submits(fake: InMemoryMarket) -> int:
    return [c[0] for c in fake.calls].count("submit")


async def test_the_agent_places_its_own_order_under_the_reserved_id():
    fake, seen = InMemoryMarket(), []
    result = await run_strategy(SPEC, fake, agent_step(fake, buys_at_first_decision(), seen))

    assert result.state is RunState.COMPLETED
    (order,) = result.orders
    assert order.result.client_order_id == reserved_for(0)
    assert (order.event_sequence, order.order_index, order.result.status) == (0, 0, "filled")
    assert order.request.client_order_id == order.result.client_order_id
    assert submits(fake) == 1 and "orders" not in [c[0] for c in fake.calls]
    assert result.decision_errors == ()
    assert len(result.marks) == len(SESSIONS)

    (request,) = seen
    bazaar = {k.lower(): v for k, v in request.headers.items() if k.lower().startswith("x-bazaar")}
    assert bazaar == {
        APPROVAL_HEADER.lower(): str(SPEC.approval_id),
        ACCOUNT_HEADER.lower(): str(order.result.account.account_id),
    }
    assert RUNNER_TOKEN_HEADER.lower() not in {k.lower() for k in request.headers}


def reserved_for(event_sequence: int) -> UUID:
    return uuid5(SPEC.experiment_id, f"decision:{event_sequence}")


async def test_a_hold_places_nothing_and_the_run_continues():
    async def hold(ctx, account, client, client_order_id, cycles=()):
        return AgentDecision(None, None, None)

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, hold))
    assert result.state is RunState.COMPLETED
    assert result.orders == () and result.decision_errors == ()
    assert submits(fake) == 0 and len(result.marks) == len(SESSIONS)


async def test_an_agent_that_raises_after_its_fill_is_reconciled_without_a_second_order():
    def blow_up():
        raise RuntimeError("model output was garbage")

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, buys_at_first_decision(blow_up)))

    assert result.state is RunState.COMPLETED
    (order,) = result.orders
    assert order.result.status == "filled" and order.event_sequence == 0
    assert submits(fake) == 1
    (error,) = result.decision_errors
    assert (error.event_sequence, error.reconciled) == (0, "found")
    assert error.client_order_id == order.result.client_order_id
    assert error.error == "the agent decision raised RuntimeError"
    assert "garbage" not in error.error


async def test_an_agent_error_after_its_fill_is_reconciled_too():
    async def errs_after_filling(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        order = buy_order(client_order_id)
        result = await post_order(client, ctx, order)
        return AgentDecision(
            order, result, "invalid_response: Model did not produce a valid decision"
        )

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, errs_after_filling))
    (order,) = result.orders
    (error,) = result.decision_errors
    assert error.reconciled == "found" and submits(fake) == 1
    assert order.result.client_order_id == error.client_order_id == reserved_for(0)
    assert error.error == "invalid_response: Model did not produce a valid decision"


async def test_an_agent_that_raises_before_ordering_is_reconciled_as_absent():
    async def raises(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence == 2:
            raise TimeoutError
        return AgentDecision(None, None, None)

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, raises))
    assert result.state is RunState.COMPLETED
    assert result.orders == ()
    (error,) = result.decision_errors
    assert (error.event_sequence, error.reconciled) == (2, "absent")
    assert len(result.marks) == len(SESSIONS)


async def test_an_order_under_another_id_is_not_trusted():
    async def wrong_id(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        order = buy_order(UUID(int=12345))
        return AgentDecision(order, await post_order(client, ctx, order), None)

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, wrong_id))
    # The market has the stray order, but not under the reserved id: the decision records none.
    assert result.orders == ()
    (error,) = result.decision_errors
    assert error.reconciled == "absent"
    assert error.error == "the agent reported an order under another id"


async def test_a_failed_reconcile_fails_the_run():
    class NoOrderList(InMemoryMarket):
        async def orders(self, ctx, start_at):
            raise httpx.ConnectError("market went away")

    async def raises(ctx, account, client, client_order_id, cycles=()):
        raise RuntimeError("boom")

    fake = NoOrderList()
    result = await run_strategy(SPEC, fake, agent_step(fake, raises))
    assert result.state is RunState.FAILED
    assert result.failure_code == "reconcile_failed"
    assert result.failure == (
        "the market's order list could not be read during the decision at"
        " 2026-02-02T14:30:00Z (event 0), so the account state after the agent's decision is"
        " unknown"
    )
    assert result.account.account_id in fake.closed


async def test_cancellation_reconciles_then_propagates():
    reconciled = []

    class Watching(InMemoryMarket):
        async def orders(self, ctx, start_at):
            reconciled.append(ctx.event_sequence)
            return await super().orders(ctx, start_at)

    async def cancelled(ctx, account, client, client_order_id, cycles=()):
        raise asyncio.CancelledError

    fake = Watching()
    with pytest.raises(asyncio.CancelledError):
        await run_strategy(SPEC, fake, agent_step(fake, cancelled))
    assert reconciled == [0]


async def test_cancellation_after_a_fill_records_it_then_propagates():
    async def fills_then_cancelled(ctx, account, client, client_order_id, cycles=()):
        await post_order(client, ctx, buy_order(client_order_id))
        raise asyncio.CancelledError

    fake = InMemoryMarket()
    orders = []
    step = agent_step(fake, fills_then_cancelled)
    ctx = await _first_decision_context(fake)
    with pytest.raises(asyncio.CancelledError):
        await step.decide(ctx, fake.accounts[ctx.account_id], fake, orders)
    (order,) = orders
    assert order.result.client_order_id == reserved_order_id(ctx) and submits(fake) == 1


async def test_cancellation_with_a_failed_reconcile_still_propagates():
    class NoOrderList(InMemoryMarket):
        async def orders(self, ctx, start_at):
            raise httpx.ConnectError("market went away")

    async def cancelled(ctx, account, client, client_order_id, cycles=()):
        raise asyncio.CancelledError

    fake = NoOrderList()
    with pytest.raises(asyncio.CancelledError):
        await run_strategy(SPEC, fake, agent_step(fake, cancelled))


async def test_an_order_sent_without_a_result_is_reconciled():
    async def lost_result(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        order = buy_order(client_order_id)
        await post_order(client, ctx, order)
        return AgentDecision(order, None, None)

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, lost_result))
    (order,) = result.orders
    (error,) = result.decision_errors
    assert error.reconciled == "found" and order.result.client_order_id == reserved_for(0)
    assert error.error == "the agent sent an order but reported no result"
    assert submits(fake) == 1


async def _first_decision_context(fake: InMemoryMarket):
    from bazaar_runner.clock import RunManifest, build_schedule

    await fake.set_cutoff(SPEC.experiment_id, SESSIONS[0].open_at, "synthetic-v1", "exec-v1")
    account = await fake.create_account(
        SPEC.experiment_id,
        SPEC.agent_id,
        SPEC.strategy_version_id,
        Decimal(10000),
        request_id=SPEC.run_id,
    )
    manifest = RunManifest(
        **SPEC.model_dump(include=set(RunManifest.model_fields) - {"account_id"}),
        account_id=account.account_id,
    )
    return build_schedule(SPEC.script)[0].context(manifest)


def test_the_reserved_id_is_deterministic_per_decision():
    from bazaar_runner.clock import RunManifest, build_schedule

    manifest = RunManifest(
        **SPEC.model_dump(include=set(RunManifest.model_fields) - {"account_id"}),
        account_id=UUID(int=1),
    )
    first, second = (
        [reserved_order_id(e.context(manifest)) for e in build_schedule(SPEC.script)]
        for _ in range(2)
    )
    assert first == second and len(set(first)) == len(first)
    assert first[0] == reserved_for(0)


async def test_agent_spans_and_files_carry_no_token(capfire, tmp_path):
    fake, seen = InMemoryMarket(), []
    client = httpx.AsyncClient(transport=delegating_transport(fake), base_url=MARKET_URL)
    market = HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, TOKEN)

    def blow_up():
        raise RuntimeError("after the fill")

    record, _ = await record_run(
        SPEC,
        market,
        agent_step(fake, buys_at_first_decision(blow_up), seen),
        policy_ref="agent-test",
        runs_dir=tmp_path,
    )
    assert record.status == "completed"
    (error,) = record.decision_errors
    assert error.reconciled == "found"
    assert (
        RunRecord.model_validate_json((tmp_path / str(SPEC.run_id) / "record.json").read_text())
        == record
    )

    spans = capfire.exporter.exported_spans_as_dict()
    first = next(
        s
        for s in spans
        if s["name"] == "runner.decision" and s["attributes"]["event_sequence"] == 0
    )
    assert first["attributes"]["agent"] is True
    assert first["attributes"]["client_order_id"] == str(error.client_order_id)
    assert first["attributes"]["reconcile"] == "found"
    assert first["attributes"]["decision_error"] == "the agent decision raised RuntimeError"
    assert TOKEN not in json.dumps(spans, default=str)
    assert all(RUNNER_TOKEN_HEADER not in r.headers for r in seen)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text()


SAMPLE = Path(__file__).parent / "fixtures" / "runrecord-v1-sample.json"


@pytest.mark.parametrize("shape", ["items", "orders", "list"])
def test_parse_order_page_accepts_each_candidate_shape(shape):
    sample = json.loads(SAMPLE.read_text())["orders"][0]["result"]
    body = {
        "items": {"items": [sample], "next_cursor": "c2"},
        "orders": {"orders": [sample]},
        "list": [sample],
    }[shape]
    results, cursor = parse_order_page(json.dumps(body).encode())
    assert [r.client_order_id for r in results] == [UUID(sample["client_order_id"])]
    assert cursor == ("c2" if shape == "items" else None)


def test_parse_order_page_reads_the_markets_history_page():
    """The market's GET orders, as checked live on demo 2d7e6e3: a HistoryPage with rejections."""
    sample = json.loads(SAMPLE.read_text())
    filled, rejected = (o["result"] for o in sample["orders"][:3:2])
    page = {
        "experiment_id": sample["manifest"]["experiment_id"],
        "account_id": sample["manifest"]["account_id"],
        "agent_id": sample["manifest"]["agent_id"],
        "strategy_version_id": sample["manifest"]["strategy_version_id"],
        "cutoff_at": "2025-07-02T13:30:00Z",
        "start_at": "2025-07-01T13:30:00Z",
        "end_at": "2025-07-02T13:30:00Z",
        "source": "market-ledger",
        "data_version": "synthetic-v1",
        "coverage": "complete",
        "items": [filled, rejected],
        "next_cursor": None,
    }
    results, cursor = parse_order_page(json.dumps(page).encode())
    assert [r.status for r in results] == ["filled", "rejected"] and cursor is None


async def test_http_orders_reads_the_route_with_the_approval_only():
    seen: list[httpx.Request] = []
    fake = InMemoryMarket()
    client = httpx.AsyncClient(transport=delegating_transport(fake, seen), base_url=MARKET_URL)
    port = HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, TOKEN)
    result = await run_strategy(SPEC, port, agent_step(fake, buys_at_first_decision()))
    assert result.state is RunState.COMPLETED
    ctx = SimpleNamespace(
        experiment_id=SPEC.experiment_id,
        account_id=result.account.account_id,
        simulated_at=SESSIONS[0].open_at,
    )
    (found,) = await port.orders(ctx, SESSIONS[0].open_at)
    request = seen[-1]
    assert (request.method, request.url.path) == (
        "GET",
        f"/experiments/{SPEC.experiment_id}/accounts/{result.account.account_id}/orders",
    )
    assert request.url.params["start_at"] == request.url.params["end_at"] == "2026-02-02T14:30:00Z"
    assert RUNNER_TOKEN_HEADER not in request.headers
    assert found.client_order_id == reserved_for(0)
    assert Decimal(found.quantity) == 3


# Duncan's real harness: these run on demo/aie-nyc, where bazaar_agent.trading exists.


def _prompt_reserved_id(messages) -> str:
    # run_decision states the reserved id in its context prompt; a model must reuse it.
    text = " ".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)
    return re.search(r"reserved client_order_id=([0-9a-f-]{36})", text).group(1)


async def test_real_harness_function_model_buys_once():
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import make_agent_decider
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    def trader(messages, info):
        returned = any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if returned:
            output = info.output_tools[0].name
            return ModelResponse(
                parts=[ToolCallPart(output, {"action": "ordered"}, tool_call_id="out")]
            )
        request = {
            "client_order_id": _prompt_reserved_id(messages),
            "symbol": "AAPL",
            "side": "buy",
            "quantity": "2",
        }
        # market_order takes the OrderRequest fields directly as its arguments.
        return ModelResponse(parts=[ToolCallPart("market_order", request, tool_call_id="buy")])

    decided = []

    async def first_only(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        decided.append(client_order_id)
        return await real(ctx, account, client, client_order_id, cycles)

    real = make_agent_decider("Buy two AAPL at the first open.", lambda ref: FunctionModel(trader))
    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, first_only))

    assert result.state is RunState.COMPLETED, result.failure
    assert result.decision_errors == ()
    (order,) = result.orders
    assert order.result.status == "filled"
    assert order.result.client_order_id == decided[0] == reserved_for(0)
    assert submits(fake) == 1


async def test_real_harness_test_model_holds():
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import make_agent_decider
    from pydantic_ai.models.test import TestModel

    decider = make_agent_decider(
        "Hold.", lambda ref: TestModel(call_tools=[], custom_output_args={"action": "hold"})
    )
    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, decider))
    assert result.state is RunState.COMPLETED, result.failure
    assert result.orders == () and result.decision_errors == ()
    assert submits(fake) == 0


async def test_real_harness_post_order_read_error_is_feedback_without_retrading():
    """A changed-history read returns invalid_response feedback, not an ambiguous order."""
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import make_agent_decider
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    window = {"start_at": "2026-02-01T00:00:00Z", "end_at": "2026-02-02T14:30:00Z"}
    feedback = []

    def trader(messages, info):
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        returns = len(returned)
        if returns == 1:
            order = {
                "client_order_id": _prompt_reserved_id(messages),
                "symbol": "AAPL",
                "side": "buy",
                "quantity": "2",
            }
            return ModelResponse(parts=[ToolCallPart("market_order", order, tool_call_id="buy")])
        if returns in (0, 2):
            return ModelResponse(
                parts=[ToolCallPart("orders", window, tool_call_id=f"read{returns}")]
            )
        feedback.append(returned[-1].content)
        return ModelResponse(
            parts=[
                ToolCallPart(info.output_tools[0].name, {"action": "ordered"}, tool_call_id="out")
            ]
        )

    real = make_agent_decider("Check orders, buy, check again.", lambda ref: FunctionModel(trader))
    outcomes = []

    async def first_only(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        outcomes.append(await real(ctx, account, client, client_order_id, cycles))
        return outcomes[-1]

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, first_only))

    (outcome,) = outcomes
    assert outcome.error is None
    assert feedback[0].error.code == "invalid_response" and feedback[0].data is None
    assert result.state is RunState.COMPLETED
    (order,) = result.orders
    assert order.result.status == "filled" and order.result.client_order_id == reserved_for(0)
    assert result.decision_errors == ()
    assert submits(fake) == 1


CYCLES = (
    FiscalCycle(symbol="AAPL", start=date(2026, 1, 1)),
    FiscalCycle(symbol="MSFT", start=date(2026, 1, 1)),
)


def capturing(seen: list):
    async def decide(ctx, account, client, client_order_id, cycles=()):
        seen.append((ctx.event_sequence, cycles))
        return AgentDecision(None, None, None)

    return decide


async def test_the_markets_fiscal_cycles_reach_every_agent_decision(capfire):
    fake, seen = InMemoryMarket(), []
    fake.cycles = CYCLES
    step = AgentStep(
        capturing(seen),
        market_url=MARKET_URL,
        symbols=("AAPL", "MSFT", "KO"),
        transport=delegating_transport(fake),
    )
    result = await run_strategy(SPEC, fake, step)

    assert result.state is RunState.COMPLETED
    assert seen == [(n, CYCLES) for n in range(0, 2 * len(SESSIONS), 2)]
    # Read per decision, after that decision's cutoff, so the cycles are as of it.
    for i, call in enumerate(fake.calls):
        if call[0] == "fiscal_cycles":
            assert fake.calls[i - 2] == ("set_cutoff", call[1])
            assert call[2] == ("AAPL", "MSFT", "KO")
    spans = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "runner.decision"]
    assert {
        (s["attributes"]["fiscal_cycles"], s["attributes"]["fiscal_cycles_read"]) for s in spans
    } == {(2, "ok")}


async def test_a_failed_fiscal_cycle_read_gives_none_and_the_run_continues(capfire):
    class NoCycles(InMemoryMarket):
        async def fiscal_cycles(self, ctx, symbols):
            raise httpx.ConnectError("route not deployed")

    fake, seen = NoCycles(), []
    step = AgentStep(capturing(seen), market_url=MARKET_URL, symbols=("AAPL",))
    result = await run_strategy(SPEC, fake, step)
    assert result.state is RunState.COMPLETED and result.decision_errors == ()
    assert {cycles for _, cycles in seen} == {()}
    spans = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "runner.decision"]
    assert {s["attributes"]["fiscal_cycles_read"] for s in spans} == {"failed"}


async def test_fiscal_cycles_can_be_switched_off():
    fake, seen = InMemoryMarket(), []
    fake.cycles = CYCLES
    step = AgentStep(
        capturing(seen), market_url=MARKET_URL, symbols=("AAPL",), read_fiscal_cycles=False
    )
    await run_strategy(SPEC, fake, step)
    assert {cycles for _, cycles in seen} == {()}
    assert "fiscal_cycles" not in [c[0] for c in fake.calls]


@pytest.mark.parametrize("wrap", [False, True])
def test_parse_fiscal_cycles_accepts_a_bare_list_or_items(wrap):
    items = [{"symbol": "AAPL", "start": "2026-01-01"}, {"symbol": "MSFT", "start": "2026-01-01"}]
    assert parse_fiscal_cycles(json.dumps({"items": items} if wrap else items).encode()) == CYCLES


async def test_http_fiscal_cycles_sends_the_symbols_and_the_approval_only():
    seen: list[httpx.Request] = []
    fake = InMemoryMarket()
    fake.cycles = CYCLES
    client = httpx.AsyncClient(transport=delegating_transport(fake, seen), base_url=MARKET_URL)
    port = HttpMarketPort(client, SPEC.experiment_id, SPEC.approval_id, TOKEN)
    ctx = SimpleNamespace(experiment_id=SPEC.experiment_id, account_id=UUID(int=1))
    assert await port.fiscal_cycles(ctx, ("AAPL", "KO")) == CYCLES[:1]
    (request,) = seen
    assert request.url.path == f"/experiments/{SPEC.experiment_id}/fiscal-cycles"
    assert request.url.params["symbols"] == "AAPL,KO"
    bazaar = {k.lower() for k in request.headers if k.lower().startswith("x-bazaar")}
    assert bazaar == {APPROVAL_HEADER.lower()}


@pytest.mark.parametrize("with_cycle", [True, False])
async def test_real_harness_filings_needs_the_markets_fiscal_cycle(with_cycle):
    pytest.importorskip("bazaar_agent.trading")
    from bazaar_runner.agent import make_agent_decider
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    feedback = []

    def reader(messages, info):
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if returned:
            feedback.append(returned[-1].content)
            final = ToolCallPart(info.output_tools[0].name, {"action": "hold"}, tool_call_id="out")
            return ModelResponse(parts=[final])
        window = {
            "symbol": "AAPL",
            "start_at": "2026-01-26T14:30:00Z",
            "end_at": "2026-02-02T14:30:00Z",
            "limit": 3,
        }
        return ModelResponse(parts=[ToolCallPart("filings", window, tool_call_id="filings")])

    real = make_agent_decider("Read filings, then hold.", lambda ref: FunctionModel(reader))

    async def first_only(ctx, account, client, client_order_id, cycles=()):
        if ctx.event_sequence != 0:
            return AgentDecision(None, None, None)
        return await real(ctx, account, client, client_order_id, cycles)

    fake = InMemoryMarket()
    fake.cycles = CYCLES if with_cycle else ()
    step = AgentStep(
        first_only,
        market_url=MARKET_URL,
        symbols=("AAPL",),
        transport=delegating_transport(fake),
    )
    result = await run_strategy(SPEC, fake, step)
    assert result.state is RunState.COMPLETED
    if with_cycle:
        # Past the "Trusted fiscal cycle unavailable" check: the read reached the market.
        assert result.decision_errors == () and feedback[0].error is None
        assert ("filings", "AAPL") in fake.calls
    else:
        assert result.decision_errors == ()
        assert feedback[0].error.code == "unsupported" and feedback[0].data is None
        assert feedback[0].error.message == "Trusted fiscal cycle unavailable"
        assert ("filings", "AAPL") not in fake.calls


async def test_agent_usage_is_recorded_per_error_and_in_total():
    from bazaar_runner.run import AgentUsage

    async def spends(ctx, account, client, client_order_id, cycles=()):
        usage = AgentUsage(model_requests=4, tool_calls=3, total_tokens=1000)
        error = "conflict: Decision budget exhausted; do not advance or retrade"
        return AgentDecision(None, None, error if ctx.event_sequence == 2 else None, usage)

    fake = InMemoryMarket()
    result = await run_strategy(SPEC, fake, agent_step(fake, spends))
    (error,) = result.decision_errors
    assert error.usage == AgentUsage(model_requests=4, tool_calls=3, total_tokens=1000)
    assert result.agent_usage == AgentUsage(
        model_requests=4 * len(SESSIONS), tool_calls=3 * len(SESSIONS), total_tokens=10_000
    )

"""DecideWithAgent backed by Duncan's harness, bazaar_agent.trading.run_decision.

bazaar_agent.trading lives on the demo integration branch, so everything that touches it is
imported when a decider is made, not when this module is imported.
"""

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

import httpx
from bazaar_protocol import AccountSnapshot, ExperimentContext

from bazaar_runner.agent_step import AgentDecision, DecideWithAgent

AGENT_FIXTURE_INSTRUCTIONS = "Inspect account and eligible prices; hold or trade once."
# A fixture strategy version: the demo does not go through the registry (A8).
FIXTURE_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE_CREATED_BY = "bazaar-runner-fixture"


def make_agent_decider(
    instructions: str,
    model_factory: Callable[[str], Any],
    *,
    budget: Any = None,
    runtime: Any = None,
    cycles: Sequence[Any] = (),
) -> DecideWithAgent:
    """`cycles` are trusted FiscalCycle records from an importer; never invented (default none)."""
    from bazaar_agent.registry_store import digest
    from bazaar_agent.research import ResearchContext
    from bazaar_agent.trading import MarketIdentity, run_decision
    from bazaar_protocol.registry import StrategyDefinition, StrategyVersion

    definition = StrategyDefinition(instructions=instructions)

    def version_for(ctx: ExperimentContext) -> StrategyVersion:
        return StrategyVersion(
            version_id=ctx.strategy_version_id,
            strategy_id=uuid5(ctx.strategy_version_id, "strategy"),
            version=1,
            definition=definition,
            parent_version_id=None,
            hypothesis="",
            definition_digest=digest(definition.model_dump(mode="json")),
            created_at=FIXTURE_CREATED_AT,
            created_by=FIXTURE_CREATED_BY,
        )

    async def decide(
        ctx: ExperimentContext,
        account: AccountSnapshot,
        client: httpx.AsyncClient,
        client_order_id: UUID,
    ) -> AgentDecision:
        result = await run_decision(
            identity=MarketIdentity(
                agent_id=ctx.agent_id,
                account_id=ctx.account_id,
                experiment_id=ctx.experiment_id,
                strategy_version_id=ctx.strategy_version_id,
            ),
            version=version_for(ctx),
            context=ResearchContext(experiment=ctx, cycles=tuple(cycles)),
            client=client,
            client_order_id=client_order_id,
            budget=budget,
            runtime=runtime,
            model_factory=model_factory,
        )
        error = f"{result.error.code}: {result.error.message}" if result.error else None
        return AgentDecision(result.order_request, result.order_result, error)

    return decide


def fixture_model_factory(symbol: str = "AAPL", quantity: int = 10) -> Callable[[str], Any]:
    """agent-fixture-v1 (A11), declared up front and never tuned on results.

    At its first decision it reads `symbol` news for the week before the decision once, then buys
    `quantity` whole shares of `symbol` under the runner-reserved id (run_decision states the id
    and the decision time in its context prompt). It never branches on the news; a news error
    ends the decision, which the runner reconciles. At every later decision it holds. One factory
    per run, since it remembers its first decision.
    """
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    first_decision_done = False

    def trade(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal first_decision_done
        parts = [p for m in messages for p in m.parts]
        returned = {p.tool_name for p in parts if isinstance(p, ToolReturnPart)}

        def output(action: str) -> ModelResponse:
            final = ToolCallPart(info.output_tools[0].name, {"action": action}, tool_call_id="out")
            return ModelResponse(parts=[final])

        if "market_order" in returned:
            return output("ordered")
        if first_decision_done and "news" not in returned:
            return output("hold")
        first_decision_done = True
        prompt = " ".join(str(getattr(p, "content", "")) for p in parts)
        if "news" not in returned:
            at = _prompt_value(prompt, r"fixed simulated time (\S+);")
            end = datetime.fromisoformat(at)
            window = {
                "symbol": symbol,
                "start_at": (end - timedelta(days=7)).isoformat(),
                "end_at": end.isoformat(),
            }
            return ModelResponse(parts=[ToolCallPart("news", window, tool_call_id="news")])
        order = {
            "client_order_id": _prompt_value(prompt, r"reserved client_order_id=([0-9a-f-]{36})"),
            "symbol": symbol,
            "side": "buy",
            "quantity": str(quantity),
        }
        return ModelResponse(parts=[ToolCallPart("market_order", order, tool_call_id="buy")])

    return lambda model_ref: FunctionModel(trade)


def _prompt_value(prompt: str, pattern: str) -> str:
    match = re.search(pattern, prompt)
    if match is None:
        raise ValueError(f"run_decision's context prompt no longer matches {pattern!r}")
    return match.group(1)

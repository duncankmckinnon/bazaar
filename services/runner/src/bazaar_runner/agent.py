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
from bazaar_runner.market import FiscalCycle
from bazaar_runner.run import AgentUsage

AGENT_FIXTURE_INSTRUCTIONS = "Inspect account and eligible prices; hold or trade once."
# A small news page: real articles cost ~1k tokens each and run_decision's default budget is 16k
# tokens, so a 100-article page exhausts it before the order (T7b, measured 37,461 tokens).
FIXTURE_NEWS_LIMIT = 3
# A fixture strategy version: the demo does not go through the registry (A8).
FIXTURE_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE_CREATED_BY = "bazaar-runner-fixture"


def redacting_model_factory(model_factory: Callable[[str], Any]) -> Callable[[str], Any]:
    """The agent's spans record a model error before the runner sees it, and Logfire never
    scrubs exception text. So an error whose text holds a configured secret leaves the model as a
    RedactedError with the secret removed and no chain; any other error is unchanged."""
    from bazaar_protocol.telemetry import redacted_exceptions
    from pydantic_ai.models.wrapper import WrapperModel

    class RedactingModel(WrapperModel):
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            with redacted_exceptions():
                return await self.wrapped.request(*args, **kwargs)

    return lambda model_ref: RedactingModel(model_factory(model_ref))


def make_agent_decider(
    instructions: str,
    model_factory: Callable[[str], Any] | None,
    *,
    budget: Any = None,
    runtime: Any = None,
) -> DecideWithAgent:
    """Fiscal cycles arrive per decision from the market (AgentStep), never invented here.
    With no model_factory the model is the operator's default (env_model_factory), as in
    run_decision; either way its errors are redacted (redacting_model_factory)."""
    from bazaar_agent.registry_store import digest
    from bazaar_agent.research import FiscalCycle as ResearchFiscalCycle
    from bazaar_agent.research import ResearchContext
    from bazaar_agent.trading import MarketIdentity, env_model_factory, run_decision
    from bazaar_protocol.registry import StrategyDefinition, StrategyVersion

    definition = StrategyDefinition(instructions=instructions)
    model_factory = redacting_model_factory(model_factory or env_model_factory())

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
        cycles: Sequence[FiscalCycle] = (),
    ) -> AgentDecision:
        trusted = tuple(ResearchFiscalCycle(symbol=c.symbol, start=c.start) for c in cycles)
        result = await run_decision(
            identity=MarketIdentity(
                agent_id=ctx.agent_id,
                account_id=ctx.account_id,
                experiment_id=ctx.experiment_id,
                strategy_version_id=ctx.strategy_version_id,
            ),
            version=version_for(ctx),
            context=ResearchContext(experiment=ctx, cycles=trusted),
            client=client,
            client_order_id=client_order_id,
            budget=budget,
            runtime=runtime,
            model_factory=model_factory,
        )
        error = f"{result.error.code}: {result.error.message}" if result.error else None
        usage = AgentUsage(
            model_requests=result.usage.model_requests,
            tool_calls=result.usage.tool_calls,
            total_tokens=result.usage.total_tokens,
        )
        return AgentDecision(result.order_request, result.order_result, error, usage)

    return decide


def fixture_model_factory(symbol: str = "AAPL", quantity: int = 10) -> Callable[[str], Any]:
    """agent-fixture-v1 (A11), declared up front and never tuned on results.

    At its first decision it reads one small page of `symbol` news (FIXTURE_NEWS_LIMIT articles)
    for the week before the decision, then buys
    `quantity` whole shares of `symbol` under the runner-reserved id (run_decision states the id
    and the decision time in its context prompt). It does not interpret news content; if the
    news read returns an error it holds instead of buying. At every later decision it holds. One factory
    per run, since it remembers its first decision.
    """
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    first_decision_done = False

    def trade(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal first_decision_done
        parts = [p for m in messages for p in m.parts]
        returns = {p.tool_name: p.content for p in parts if isinstance(p, ToolReturnPart)}
        returned = set(returns)

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
                "limit": FIXTURE_NEWS_LIMIT,
            }
            return ModelResponse(parts=[ToolCallPart("news", window, tool_call_id="news")])
        if returns["news"].error is not None:
            return output("hold")
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

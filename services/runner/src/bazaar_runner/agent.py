"""DecideWithAgent backed by Duncan's harness, bazaar_agent.trading.run_decision.

bazaar_agent.trading lives on the demo integration branch, so everything that touches it is
imported when a decider is made, not when this module is imported.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

import httpx
from bazaar_protocol import AccountSnapshot, ExperimentContext

from bazaar_runner.agent_step import AgentDecision, DecideWithAgent

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

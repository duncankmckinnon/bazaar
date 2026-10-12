"""The strategist as a pydantic-evals experiment (v10 D1): frozen cases, deterministic evaluators,
and a task that varies the role (prompt), the model and K.

Each evaluator scores the fraction of the model's RAW drafts that pass one rule, using the
runtime gate's own predicates (bazaar_agent.strategist), so a variant is compared on what it
drafted and not only on what validation let through. The task opens a strategist.eval_case span,
never strategist.propose and never a bazaar.run_id or bazaar.strategy_name attribute, so
dashboards and brief() never count experiment output.
"""

import logging
import os
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import logfire
from pydantic import BaseModel, ConfigDict
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from bazaar_agent.strategist import (
    STRATEGIST_ROLE,
    Draft,
    PerformanceBrief,
    Proposal,
    StrategistError,
    allowed_symbols,
    draft_proposals,
    is_multi_position,
    makes_claim,
    near_duplicate,
    normalized,
    qualified_name,
    tickers_allowed,
    valid_length,
    valid_name,
)

logger = logging.getLogger(__name__)

DATASET_NAME = "bazaar-strategist"
WRITE_KEY_ENV = "LOGFIRE_API_WRITE_KEY"
# The variant arm: the same role, plus one instruction to contrast the top with the bottom.
PROMPT_VARIANT = (
    STRATEGIST_ROLE + " Before writing, find what the top performers share that the bottom ones "
    "lack, build each strategy on one shared trait of the top, and write rules a trader can "
    "carry out with one order per decision."
)


class StrategistCase(BaseModel):
    """Everything one propose() call reads, frozen: a case is self-contained."""

    model_config = ConfigDict(frozen=True)

    brief: PerformanceBrief
    k: int
    generation: int
    instructions_by_name: dict[str, str]
    existing_instructions: list[str]
    universe: list[dict]
    benchmark: str | None
    trading_days: int | None = None


class StrategistOutput(BaseModel):
    drafts: list[Draft]
    proposals: list[Proposal]
    k: int  # the K this run asked for (the case's, or the task's override)


Context = EvaluatorContext[StrategistCase, StrategistOutput, None]


def _fraction(drafts: list[Draft], passes: Callable[[Draft], bool]) -> float:
    return sum(passes(d) for d in drafts) / len(drafts) if drafts else 0.0


@dataclass
class Executable(Evaluator[StrategistCase, StrategistOutput, None]):
    """A trader can run it under the runtime constraints: length, claim, ticker, multi_position."""

    def evaluate(self, ctx: Context) -> float:
        allowed = allowed_symbols(ctx.inputs.universe, ctx.inputs.benchmark)

        def passes(draft: Draft) -> bool:
            text = draft.instructions.strip()
            return (
                valid_length(text)
                and not makes_claim(text)
                and not is_multi_position(text)
                and tickers_allowed(text, allowed)
            )

        return _fraction(ctx.output.drafts, passes)


@dataclass
class NoMultiPosition(Evaluator[StrategistCase, StrategistOutput, None]):
    def evaluate(self, ctx: Context) -> float:
        return _fraction(ctx.output.drafts, lambda d: not is_multi_position(d.instructions))


@dataclass
class DistinctNames(Evaluator[StrategistCase, StrategistOutput, None]):
    """A valid name, unique in the batch and not a brief name."""

    def evaluate(self, ctx: Context) -> float:
        names = [qualified_name(d.name, ctx.inputs.generation) for d in ctx.output.drafts]
        counts, taken = Counter(names), set(ctx.inputs.brief.names)
        return _fraction(
            ctx.output.drafts,
            lambda d: (
                valid_name(name := qualified_name(d.name, ctx.inputs.generation))
                and counts[name] == 1
                and name not in taken
            ),
        )


@dataclass
class ParentsInBrief(Evaluator[StrategistCase, StrategistOutput, None]):
    """At least one parent, and EVERY listed parent is a brief name."""

    def evaluate(self, ctx: Context) -> float:
        names = set(ctx.inputs.brief.names)
        return _fraction(
            ctx.output.drafts, lambda d: bool(d.parents) and all(p in names for p in d.parents)
        )


@dataclass
class NoParentCopy(Evaluator[StrategistCase, StrategistOutput, None]):
    """Not a near-duplicate of any brief strategy's instructions shown to the model."""

    def evaluate(self, ctx: Context) -> float:
        parents = [
            normalized(text)
            for name, text in ctx.inputs.instructions_by_name.items()
            if name in ctx.inputs.brief.names
        ]
        return _fraction(
            ctx.output.drafts, lambda d: not near_duplicate(d.instructions.strip(), parents)
        )


@dataclass
class DeliveredK(Evaluator[StrategistCase, StrategistOutput, None]):
    def evaluate(self, ctx: Context) -> bool:
        return len(ctx.output.proposals) >= ctx.output.k


EVALUATORS = (
    Executable(),
    NoMultiPosition(),
    DistinctNames(),
    ParentsInBrief(),
    NoParentCopy(),
    DeliveredK(),
)


def make_task(
    *, role: str = STRATEGIST_ROLE, model: str | None = None, k: int | None = None
) -> Callable[[StrategistCase], Awaitable[StrategistOutput]]:
    """An evaluation task: one draft_proposals call per case with this role, model and K
    (None: the case's own K)."""

    async def strategist_task(case: StrategistCase) -> StrategistOutput:
        wanted = case.k if k is None else k
        with logfire.span("strategist.eval_case", k=wanted, generation=case.generation):
            drafts, proposals, _ = await draft_proposals(
                brief=case.brief,
                k=wanted,
                generation=case.generation,
                instructions_by_name=case.instructions_by_name,
                existing_instructions=case.existing_instructions,
                universe=case.universe,
                benchmark=case.benchmark,
                trading_days=case.trading_days,
                role=role,
                model=model,
            )
        return StrategistOutput(drafts=drafts, proposals=proposals, k=wanted)

    return strategist_task


def dataset(cases: Mapping[str, StrategistCase]) -> Dataset[StrategistCase, StrategistOutput, None]:
    """The named cases as the bazaar-strategist dataset, with every evaluator attached."""
    return Dataset[StrategistCase, StrategistOutput, None](
        name=DATASET_NAME,
        cases=[Case(name=name, inputs=case) for name, case in cases.items()],
        evaluators=list(EVALUATORS),
    )


def push_dataset(
    ds: Dataset[StrategistCase, StrategistOutput, None],
    *,
    description: str = "Frozen strategist cases for the house lane (v10 D1).",
) -> str:
    """Publish the dataset to Logfire with $LOGFIRE_API_WRITE_KEY, read only here.

    No key: skipped (logged and returned), never an error. A failure raises StrategistError
    naming only the error's type: never the key or a response."""
    key = os.environ.get(WRITE_KEY_ENV)
    if not key:
        message = f"skipped: {WRITE_KEY_ENV} not set"
        logger.info("strategist dataset push %s", message)
        return message
    from logfire.experimental.api_client import LogfireAPIClient

    failure: StrategistError | None = None
    try:
        with LogfireAPIClient(api_key=key) as client:
            client.push_dataset(ds, description=description)
    except Exception as exc:  # noqa: BLE001 - only the type: details could quote the key
        failure = StrategistError(f"the dataset push failed ({type(exc).__name__})")
    if failure is not None:
        raise failure  # outside the except block: the original error is not its context
    return f"pushed {ds.name} ({len(ds.cases)} cases)"

"""Comparison batches: which runs are compared, and whether they are matched."""

from collections import defaultdict
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from bazaar_protocol import NonNegativeAmount, PositiveAmount, Version, WireModel
from bazaar_protocol.registry import Tool
from pydantic import AwareDatetime, Field, model_validator


class BatchKind(StrEnum):
    REPLAY = "replay"
    COMPARISON = "comparison"


class RunRole(StrEnum):
    CANDIDATE = "candidate"
    BASELINE = "baseline"


class RunDescriptor(WireModel):
    """One independently accounted run in a comparison batch.

    `budget` is the run's model-spend cap in USD, separate from market `starting_capital`.
    `seed` is recorded for reproducibility but not matched: repetitions may use different seeds.
    """

    role: RunRole
    policy_ref: Version
    start_at: AwareDatetime
    end_at: AwareDatetime
    starting_capital: PositiveAmount
    data_version: Version
    execution_rule_version: Version
    evaluator_version: Version
    mark_schedule_digest: Annotated[str, Field(min_length=1)]
    tools: frozenset[Tool] = frozenset()
    budget: NonNegativeAmount = Decimal(0)
    seed: Annotated[int, Field(ge=0, strict=True)]
    repetition: Annotated[int, Field(ge=0, strict=True)]

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class MismatchCode(StrEnum):
    STARTING_CAPITAL = "starting_capital"
    PERIOD_START = "period_start"
    PERIOD_END = "period_end"
    DATA_VERSION = "data_version"
    EXECUTION_RULE_VERSION = "execution_rule_version"
    EVALUATOR_VERSION = "evaluator_version"
    MARK_SCHEDULE = "mark_schedule"
    REPETITION_COUNT = "repetition_count"
    REPETITION_INDEX = "repetition_index"
    ACCESS = "access"
    BUDGET = "budget"


class ComparisonMismatch(ValueError):
    def __init__(self, code: MismatchCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class MatchedComparison(WireModel):
    """A spec that passed matching; lists baselines whose access or budget differ from candidates."""

    repetitions: Annotated[int, Field(ge=1, strict=True)]
    candidates: tuple[Version, ...]
    baselines: tuple[Version, ...]
    access_budget_exempt: tuple[Version, ...]


# Fields every run must share, in the order they are checked.
SHARED_FIELDS = (
    ("starting_capital", MismatchCode.STARTING_CAPITAL),
    ("start_at", MismatchCode.PERIOD_START),
    ("end_at", MismatchCode.PERIOD_END),
    ("data_version", MismatchCode.DATA_VERSION),
    ("execution_rule_version", MismatchCode.EXECUTION_RULE_VERSION),
    ("evaluator_version", MismatchCode.EVALUATOR_VERSION),
    ("mark_schedule_digest", MismatchCode.MARK_SCHEDULE),
)
# Fields shared by candidates only; baselines need no tools or model budget.
CANDIDATE_FIELDS = (("tools", MismatchCode.ACCESS), ("budget", MismatchCode.BUDGET))


class ComparisonSpec(WireModel):
    kind: ClassVar[BatchKind] = BatchKind.COMPARISON

    runs: Annotated[tuple[RunDescriptor, ...], Field(min_length=2)]

    @model_validator(mode="after")
    def well_formed(self) -> Self:
        keys = [(run.policy_ref, run.repetition) for run in self.runs]
        if len(set(keys)) != len(keys):
            raise ValueError("each policy_ref and repetition pair must appear once")
        roles = {}
        for run in self.runs:
            if roles.setdefault(run.policy_ref, run.role) != run.role:
                raise ValueError(f"policy {run.policy_ref} cannot be both candidate and baseline")
        if RunRole.CANDIDATE not in roles.values():
            raise ValueError("a comparison needs at least one candidate run")
        return self

    def policies(self, role: RunRole) -> tuple[str, ...]:
        return tuple(sorted({run.policy_ref for run in self.runs if run.role is role}))

    def validate_matched(self) -> MatchedComparison:
        for field, code in SHARED_FIELDS:
            values = {getattr(run, field) for run in self.runs}
            if len(values) > 1:
                raise ComparisonMismatch(code, f"runs differ in {field}")

        repetitions = defaultdict(set)
        for run in self.runs:
            repetitions[run.policy_ref].add(run.repetition)
        counts = {policy: len(indices) for policy, indices in repetitions.items()}
        if len(set(counts.values())) > 1:
            raise ComparisonMismatch(
                MismatchCode.REPETITION_COUNT, f"repetitions per policy differ: {counts}"
            )
        # S3 pairs runs by repetition index, so every policy must use exactly 0..n-1.
        n = len(repetitions[self.runs[0].policy_ref])
        if any(indices != set(range(n)) for indices in repetitions.values()):
            raise ComparisonMismatch(
                MismatchCode.REPETITION_INDEX, f"every policy must use repetitions 0..{n - 1}"
            )

        candidates = [run for run in self.runs if run.role is RunRole.CANDIDATE]
        for field, code in CANDIDATE_FIELDS:
            if len({getattr(run, field) for run in candidates}) > 1:
                raise ComparisonMismatch(code, f"candidate runs differ in {field}")

        reference = candidates[0]
        exempt = {
            run.policy_ref
            for run in self.runs
            if run.role is RunRole.BASELINE
            and any(
                getattr(run, field) != getattr(reference, field) for field, _ in CANDIDATE_FIELDS
            )
        }
        return MatchedComparison(
            repetitions=n,
            candidates=self.policies(RunRole.CANDIDATE),
            baselines=self.policies(RunRole.BASELINE),
            access_budget_exempt=tuple(sorted(exempt)),
        )

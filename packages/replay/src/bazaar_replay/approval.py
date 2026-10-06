"""Approval scope for replay and comparison batches."""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from bazaar_protocol import Version, WireModel
from pydantic import AwareDatetime, Field, model_validator

from bazaar_replay.comparison import BatchKind, ComparisonSpec


class ScopedApproval(WireModel):
    """Local stand-in for Duncan's approval grant (#18); replace it when #18 lands.

    `expires_at` and `revoked_at` are wall-clock times, not simulated time.
    """

    approval_id: UUID
    kind: BatchKind
    start_at: AwareDatetime
    end_at: AwareDatetime
    data_version: Version
    execution_rule_version: Version
    evaluator_version: Version
    candidates: frozenset[Version]
    baselines: frozenset[Version]
    run_limit: Annotated[int, Field(ge=0, strict=True)]
    runs_consumed: Annotated[int, Field(ge=0, strict=True)] = 0
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent_scope(self) -> Self:
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        if self.runs_consumed > self.run_limit:
            raise ValueError("runs_consumed cannot exceed run_limit")
        return self

    @property
    def runs_remaining(self) -> int:
        return self.run_limit - self.runs_consumed


class ScopeCode(StrEnum):
    REVOKED = "revoked"
    EXPIRED = "expired"
    WRONG_KIND = "wrong_kind"
    PERIOD = "period"
    VERSION = "version"
    CANDIDATES = "candidates"
    BASELINES = "baselines"
    SCOPE_EXCEEDED = "scope_exceeded"


class ScopeRejected(ValueError):
    def __init__(self, code: ScopeCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def check_batch_scope(grant: ScopedApproval, spec: ComparisonSpec, *, now: datetime) -> None:
    """Raise unless `grant` authorizes launching every run in `spec` at wall-clock `now`.

    Matching is checked first, so a grant never authorizes an unmatched batch.
    This checks scope only; consuming it belongs to whoever launches the runs.
    """
    if now.utcoffset() != timedelta(0):
        raise ValueError("now must be a UTC datetime")
    matched = spec.validate_matched()

    if grant.revoked_at is not None:
        raise ScopeRejected(ScopeCode.REVOKED, f"approval revoked at {grant.revoked_at}")
    if now >= grant.expires_at:
        raise ScopeRejected(ScopeCode.EXPIRED, f"approval expired at {grant.expires_at}")
    if grant.kind is not spec.kind:
        raise ScopeRejected(ScopeCode.WRONG_KIND, f"approval is for {grant.kind}, not {spec.kind}")

    run = spec.runs[0]
    if (run.start_at, run.end_at) != (grant.start_at, grant.end_at):
        raise ScopeRejected(ScopeCode.PERIOD, "batch period differs from the approved period")
    for field in ("data_version", "execution_rule_version", "evaluator_version"):
        if getattr(run, field) != getattr(grant, field):
            raise ScopeRejected(ScopeCode.VERSION, f"batch {field} differs from the approval")
    if frozenset(matched.candidates) != grant.candidates:
        raise ScopeRejected(ScopeCode.CANDIDATES, "batch candidates differ from the approved set")
    if frozenset(matched.baselines) != grant.baselines:
        raise ScopeRejected(ScopeCode.BASELINES, "batch baselines differ from the approved set")
    if len(spec.runs) > grant.runs_remaining:
        raise ScopeRejected(
            ScopeCode.SCOPE_EXCEEDED,
            f"batch has {len(spec.runs)} runs; approval has {grant.runs_remaining} left",
        )

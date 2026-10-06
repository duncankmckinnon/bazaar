"""Leaderboard model: join each run's RunRecord with its RunEvaluation, match, and rank.

Reads `<runs_dir>/<run_id>/record.json` (runner RunRecord v1) and `evaluation.json` (evals
RunEvaluation) through local read-models, so neither bazaar_runner nor bazaar_evaluation is
imported. Every run directory produces exactly one entry; nothing is dropped.
"""

from collections import Counter
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from bazaar_protocol import ExactAmount, WireModel
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from bazaar_replay.comparison import MismatchCode

# Runner policy_ref names: scripted-momentum-v1, baseline-buy-and-hold, baseline-cash-only.
BASELINE_PREFIX = "baseline-"
REFERENCE_PREFIX = "baseline-buy-and-hold"
SCORE_STATUSES = ("scored", "pending", "failed", "unsupported")


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Manifest(ReadModel):
    experiment_id: UUID
    account_id: UUID
    strategy_version_id: UUID
    data_version: str
    execution_rule_version: str
    schedule_digest: str
    period_start: AwareDatetime
    period_end: AwareDatetime
    starting_cash: ExactAmount
    policy_ref: str
    trace_id: str | None = None


class OrderOutcome(ReadModel):
    status: Literal["filled", "rejected"]


class OrderEntry(ReadModel):
    result: OrderOutcome


class RunRecord(ReadModel):
    manifest: Manifest
    status: Literal["completed", "failed"]
    failure: str | None = None
    orders: tuple[OrderEntry, ...] = ()
    trace_id: str | None = None


class StatusOnly(ReadModel):
    status: str


class Period(ReadModel):
    status: str
    period_return: ExactAmount | None = None
    reconciled: bool = False


class RunEvaluation(ReadModel):
    experiment_id: UUID
    account_id: UUID
    evaluator_version: str
    run_status: Literal["completed", "failed"]
    run_failure: str | None = None
    trade_scores: tuple[StatusOnly, ...] = ()
    period: Period


class Section(StrEnum):
    RANKED = "ranked"
    FAILED = "failed"
    NOT_COMPARABLE = "not_comparable"
    INVALID = "invalid"


class Entry(WireModel):
    run_id: str
    section: Section
    reason: str | None = None
    mismatch: MismatchCode | None = None
    policy_ref: str | None = None
    kind: Literal["agent", "baseline"] | None = None
    is_reference: bool = False
    strategy_version_id: UUID | None = None
    trace_id: str | None = None
    period_return: ExactAmount | None = None
    period_status: str | None = None
    not_reconciled: bool = False
    excess_vs_buy_and_hold: ExactAmount | None = None
    excess_computed: bool = False
    orders_filled: int = 0
    orders_rejected: int = 0
    trade_scores: dict[str, int] = Field(default_factory=dict)


class BoardHeader(WireModel):
    start_at: AwareDatetime
    end_at: AwareDatetime
    starting_cash: ExactAmount
    data_version: str
    schedule_digest: str


class Leaderboard(WireModel):
    header: BoardHeader | None
    reference_run_id: str | None
    ranked: tuple[Entry, ...]
    failed: tuple[Entry, ...]
    not_comparable: tuple[Entry, ...]
    invalid: tuple[Entry, ...]


class Run(ReadModel):
    run_id: str
    record: RunRecord
    evaluation: RunEvaluation

    @property
    def policy_ref(self) -> str:
        return self.record.manifest.policy_ref

    @property
    def is_baseline(self) -> bool:
        return self.policy_ref.startswith(BASELINE_PREFIX)

    @property
    def is_reference(self) -> bool:
        return self.policy_ref.startswith(REFERENCE_PREFIX)

    @property
    def failure(self) -> str | None:
        if self.record.status == "completed" and self.evaluation.run_status == "completed":
            return None
        return self.record.failure or self.evaluation.run_failure or "run failed without a reason"


def load_run(run_dir: Path) -> Run | Entry:
    """Parse one run directory, or return an invalid entry explaining why it cannot be used."""
    run_id = run_dir.name
    parsed = {}
    for name, model in (("record.json", RunRecord), ("evaluation.json", RunEvaluation)):
        try:
            parsed[name] = model.model_validate_json((run_dir / name).read_bytes())
        except FileNotFoundError:
            return Entry(run_id=run_id, section=Section.INVALID, reason=f"{name} is missing")
        except (OSError, ValidationError) as exc:
            reason = f"{name} is unreadable: {exc}".splitlines()[0]
            return Entry(run_id=run_id, section=Section.INVALID, reason=reason)
    record, evaluation = parsed["record.json"], parsed["evaluation.json"]
    for field in ("experiment_id", "account_id"):
        if getattr(record.manifest, field) != getattr(evaluation, field):
            reason = f"record and evaluation have different {field}"
            return Entry(run_id=run_id, section=Section.INVALID, reason=reason)
    return Run(run_id=run_id, record=record, evaluation=evaluation)


def mismatch(run: Run, reference: Run) -> MismatchCode | None:
    """The first way `run` is not comparable with `reference`, using S1's codes."""
    checks = (
        ("starting_cash", MismatchCode.STARTING_CAPITAL),
        ("period_start", MismatchCode.PERIOD_START),
        ("period_end", MismatchCode.PERIOD_END),
        ("schedule_digest", MismatchCode.MARK_SCHEDULE),
        ("data_version", MismatchCode.DATA_VERSION),
        ("execution_rule_version", MismatchCode.EXECUTION_RULE_VERSION),
    )
    for field, code in checks:
        if getattr(run.record.manifest, field) != getattr(reference.record.manifest, field):
            return code
    if run.evaluation.evaluator_version != reference.evaluation.evaluator_version:
        return MismatchCode.EVALUATOR_VERSION
    return None


def excess_vs_buy_and_hold(run: Run, reference: Run | None) -> Decimal | None:
    """Computed until evals publishes a baseline-relative field; switch to it here."""
    if reference is None or run is reference:
        return None
    mine, theirs = run.evaluation.period.period_return, reference.evaluation.period.period_return
    if mine is None or theirs is None:
        return None
    return mine - theirs


def entry(run: Run, section: Section, **fields: object) -> Entry:
    orders = Counter(order.result.status for order in run.record.orders)
    scores = Counter(score.status for score in run.evaluation.trade_scores)
    return Entry(
        run_id=run.run_id,
        section=section,
        policy_ref=run.policy_ref,
        kind="baseline" if run.is_baseline else "agent",
        strategy_version_id=run.record.manifest.strategy_version_id,
        trace_id=run.record.trace_id or run.record.manifest.trace_id,
        period_return=run.evaluation.period.period_return,
        period_status=run.evaluation.period.status,
        not_reconciled=not run.evaluation.period.reconciled,
        orders_filled=orders["filled"],
        orders_rejected=orders["rejected"],
        trade_scores={status: scores[status] for status in (*SCORE_STATUSES, *scores)},
        **fields,
    )


def short_digest(digest: str) -> str:
    head, sep, tail = digest.rpartition(":")
    return f"{head}{sep}{tail[:8]}" if sep else digest[:12]


def build_board(loaded: list[Run | Entry]) -> Leaderboard:
    runs = [item for item in loaded if isinstance(item, Run)]
    invalid = [item for item in loaded if isinstance(item, Entry)]

    references = [run for run in runs if run.is_reference]
    reference = next((run for run in references if run.failure is None), None)
    if reference is None and references:
        reference = references[0]
    # Without a buy-and-hold run, anchor matching on the first completed run so that
    # mismatched runs are still never ranked side by side.
    anchor = reference or next((run for run in runs if run.failure is None), None)

    ranked, failed, not_comparable = [], [], []
    for run in runs:
        is_reference = run is reference
        if run.failure is not None:
            failed.append(entry(run, Section.FAILED, reason=run.failure, is_reference=is_reference))
        elif anchor is not None and (code := mismatch(run, anchor)) is not None:
            not_comparable.append(
                entry(run, Section.NOT_COMPARABLE, mismatch=code, reason=f"differs in {code}")
            )
        else:
            usable = reference if reference is not None and reference.failure is None else None
            excess = excess_vs_buy_and_hold(run, usable)
            ranked.append(
                entry(
                    run,
                    Section.RANKED,
                    is_reference=is_reference,
                    excess_vs_buy_and_hold=excess,
                    excess_computed=excess is not None,
                )
            )

    ranked.sort(key=lambda e: e.run_id)
    ranked.sort(key=lambda e: (e.period_return is None, -(e.period_return or 0)))
    header = None
    if anchor is not None:
        manifest = anchor.record.manifest
        header = BoardHeader(
            start_at=manifest.period_start,
            end_at=manifest.period_end,
            starting_cash=manifest.starting_cash,
            data_version=manifest.data_version,
            schedule_digest=short_digest(manifest.schedule_digest),
        )
    return Leaderboard(
        header=header,
        reference_run_id=reference.run_id if reference else None,
        ranked=tuple(ranked),
        failed=tuple(failed),
        not_comparable=tuple(not_comparable),
        invalid=tuple(invalid),
    )


def load_board(runs_dir: Path) -> Leaderboard:
    run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    return build_board([load_run(path) for path in run_dirs])

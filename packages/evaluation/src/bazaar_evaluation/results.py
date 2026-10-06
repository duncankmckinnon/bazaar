from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from bazaar_protocol import ExactAmount, NonNegativeAmount, OrderSide, Symbol, Version
from pydantic import Field, model_validator

from bazaar_evaluation._base import EvaluationModel


class ScoreStatus(StrEnum):
    PENDING = "pending"
    SCORED = "scored"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class Evidence(EvaluationModel):
    order_id: UUID | None = None
    data_version: Version | None = None
    execution_rule_version: Version | None = None
    source: Version | None = None
    reason: str = ""


class Denominator(EvaluationModel):
    name: Annotated[str, Field(min_length=1, pattern=r"\S")]
    value: NonNegativeAmount


class _Result(EvaluationModel):
    status: ScoreStatus
    evaluator_version: Version
    evidence: tuple[Evidence, ...] = ()
    denominators: tuple[Denominator, ...] = ()

    @model_validator(mode="after")
    def unscored_results_explain_why(self) -> Self:
        if self.status == ScoreStatus.SCORED and not self.evidence:
            raise ValueError("a scored result needs evidence")
        if self.status in (ScoreStatus.FAILED, ScoreStatus.UNSUPPORTED) and not any(
            e.reason.strip() for e in self.evidence
        ):
            raise ValueError(f"a {self.status} result needs evidence with a reason")
        return self


class TradeScore(_Result):
    order_id: UUID
    symbol: Symbol
    side: OrderSide
    realized_pnl: ExactAmount | None = None  # net of this sell's fee and the closed lots' buy fees
    closed_quantity: NonNegativeAmount | None = None
    fee: NonNegativeAmount | None = None


class PeriodSummary(_Result):
    """Whole-run money summary. `fees` are already inside realized and unrealized PnL."""

    account_id: UUID
    experiment_id: UUID
    start_value: ExactAmount | None = None
    end_value: ExactAmount | None = None  # cash + per-holding values under the valuation rule
    market_end_value: ExactAmount | None = None  # the last mark's portfolio_value
    realized_pnl: ExactAmount | None = None
    unrealized_pnl: ExactAmount | None = None
    fees: NonNegativeAmount | None = None
    net_pnl: ExactAmount | None = None
    period_return: ExactAmount | None = None
    excess_return_vs_cash: ExactAmount | None = None  # the cash-only baseline returns 0
    max_drawdown: ExactAmount | None = None
    max_drawdown_fraction: ExactAmount | None = None
    reconciled: bool = False


class RunEvaluation(EvaluationModel):
    experiment_id: UUID
    account_id: UUID
    agent_id: UUID
    strategy_version_id: UUID
    approval_id: UUID
    data_version: Version
    execution_rule_version: Version
    evaluator_version: Version
    run_status: Literal["completed", "failed"]
    run_failure: str | None = None
    trade_scores: tuple[TradeScore, ...]
    period: PeriodSummary

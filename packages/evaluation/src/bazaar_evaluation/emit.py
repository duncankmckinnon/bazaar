"""Emit a RunEvaluation to Logfire: one span per run, one child span per trade score.

The caller configures Logfire; this module never calls `logfire.configure`.
"""

from decimal import Decimal
from enum import StrEnum
from uuid import UUID

import logfire

from bazaar_evaluation.results import Evidence, RunEvaluation, TradeScore


def _attributes(**values: object) -> dict[str, object]:
    """Drop None and make values exportable. Logfire exports Decimal as a JSON string, so money
    becomes float here, for SQL sums and charts; the exact Decimals stay in RunEvaluation."""
    converted: dict[str, object] = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, StrEnum):
            value = value.value
        elif isinstance(value, UUID):
            value = str(value)
        converted[name] = value
    return converted


def _reasons(evidence: tuple[Evidence, ...]) -> list[str]:
    return [e.reason for e in evidence if e.reason]


def _trade_attributes(score: TradeScore) -> dict[str, object]:
    links = score.evidence[0] if score.evidence else Evidence()
    return _attributes(
        order_id=score.order_id,
        symbol=score.symbol,
        side=score.side,
        status=score.status,
        realized_pnl=score.realized_pnl,
        closed_quantity=score.closed_quantity,
        fee=score.fee,
        reason="; ".join(_reasons(score.evidence)) or None,
        data_version=links.data_version,
        execution_rule_version=links.execution_rule_version,
        source=links.source,
        evaluator_version=score.evaluator_version,
    )


def emit(evaluation: RunEvaluation) -> None:
    period = evaluation.period
    run = _attributes(
        experiment_id=evaluation.experiment_id,
        account_id=evaluation.account_id,
        strategy_version_id=evaluation.strategy_version_id,
        approval_id=evaluation.approval_id,
        data_version=evaluation.data_version,
        execution_rule_version=evaluation.execution_rule_version,
        evaluator_version=evaluation.evaluator_version,
        run_status=evaluation.run_status,
        run_failure=evaluation.run_failure,
        period_status=period.status,
        reconciled=period.reconciled,
        start_value=period.start_value,
        end_value=period.end_value,
        market_end_value=period.market_end_value,
        realized_pnl=period.realized_pnl,
        unrealized_pnl=period.unrealized_pnl,
        fees=period.fees,
        net_pnl=period.net_pnl,
        period_return=period.period_return,
        excess_return_vs_cash=period.excess_return_vs_cash,
        max_drawdown=period.max_drawdown,
        max_drawdown_fraction=period.max_drawdown_fraction,
        period_reasons=None if period.reconciled else _reasons(period.evidence),
        **{f"denominator_{d.name}": d.value for d in period.denominators},
    )
    with logfire.span("evaluate run {experiment_id}", _tags=["evaluation"], **run):
        for score in evaluation.trade_scores:
            with logfire.span(
                "trade {symbol} {side} {status}",
                _tags=["evaluation"],
                **_trade_attributes(score),
            ):
                pass

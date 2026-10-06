"""Evaluate one finished run: a score per order plus a reconciled period summary."""

from decimal import Decimal

from bazaar_protocol import AccountSnapshot, FilledOrder, PortfolioSnapshot

from bazaar_evaluation.config import EvaluatorConfig
from bazaar_evaluation.inputs import RunEvidence, RunOutcome
from bazaar_evaluation.ledger import LedgerReplay, replay_ledger
from bazaar_evaluation.results import (
    Denominator,
    Evidence,
    PeriodSummary,
    RunEvaluation,
    ScoreStatus,
)


def evaluate_run(
    evidence: RunEvidence, outcome: RunOutcome, config: EvaluatorConfig
) -> RunEvaluation:
    context = evidence.context
    for snap in (outcome.final_account, *outcome.marks):
        if snap is None:
            continue
        if (snap.account_id, snap.experiment_id) != (context.account_id, context.experiment_id):
            raise ValueError(
                "run outcome snapshots must belong to the run's account and experiment"
            )
    replay = replay_ledger(evidence, config)
    return RunEvaluation(
        experiment_id=context.experiment_id,
        account_id=context.account_id,
        agent_id=context.agent_id,
        strategy_version_id=context.strategy_version_id,
        approval_id=context.approval_id,
        data_version=context.data_version,
        execution_rule_version=context.execution_rule_version,
        evaluator_version=config.evaluator_version,
        run_status=outcome.run_status,
        run_failure=outcome.run_failure,
        trade_scores=replay.trade_scores,
        period=_period(evidence, outcome, config, replay),
    )


def _holdings(snapshot: AccountSnapshot | PortfolioSnapshot) -> dict[str, Decimal]:
    return {h.symbol: h.quantity for h in snapshot.holdings}


def _value(mark: PortfolioSnapshot, config: EvaluatorConfig) -> tuple[Decimal, bool]:
    """Cash plus per-holding values rounded under the mark's valuation rule, if known."""
    rule = config.valuation_rules.get(mark.valuation_rule_version)
    held = sum(
        (
            (rule.apply(h.quantity * h.unit_mark) if rule else h.quantity * h.unit_mark)
            for h in mark.holdings
        ),
        Decimal(0),
    )
    return mark.cash + held, rule is not None


def _drawdown(values: list[Decimal]) -> tuple[Decimal, Decimal | None]:
    """Largest peak-to-trough fall in amount, and largest as a fraction of its peak."""
    peak, amount, fraction = values[0], Decimal(0), None
    for value in values:
        peak = max(peak, value)
        amount = max(amount, peak - value)
        if peak > 0:
            fraction = max(fraction or Decimal(0), (peak - value) / peak)
    return amount, fraction


def period_denominators(
    orders: tuple, statuses: list[ScoreStatus], marks: int
) -> tuple[Denominator, ...]:
    fills = sum(isinstance(o, FilledOrder) for o in orders)
    return tuple(
        Denominator(name=name, value=value)
        for name, value in (
            ("orders", len(orders)),
            ("fills", fills),
            ("rejections", len(orders) - fills),
            ("scored", statuses.count(ScoreStatus.SCORED)),
            ("failed", statuses.count(ScoreStatus.FAILED)),
            ("unsupported", statuses.count(ScoreStatus.UNSUPPORTED)),
            ("marks", marks),
        )
    )


def _period(
    evidence: RunEvidence, outcome: RunOutcome, config: EvaluatorConfig, replay: LedgerReplay
) -> PeriodSummary:
    context, opening, marks = evidence.context, evidence.opening_account, outcome.marks
    fills = [o for o in evidence.orders if isinstance(o, FilledOrder)]
    statuses = [s.status for s in replay.trade_scores]
    denominators = period_denominators(evidence.orders, statuses, len(marks))
    links = Evidence(
        data_version=context.data_version,
        execution_rule_version=context.execution_rule_version,
        source=marks[-1].source if marks else None,
    )
    notes = [f"run failed: {outcome.run_failure}"] if outcome.run_status == "failed" else []

    def summary(status: ScoreStatus, gaps: list[str], **values) -> PeriodSummary:
        return PeriodSummary(
            account_id=context.account_id,
            experiment_id=context.experiment_id,
            status=status,
            evaluator_version=config.evaluator_version,
            evidence=(
                links,
                *(Evidence(reason=n) for n in notes),
                *(Evidence(reason=f"not reconciled: {g}") for g in gaps),
            ),
            denominators=denominators,
            reconciled=status == ScoreStatus.SCORED and not gaps,
            **values,
        )

    if opening.holdings:
        notes.append("opening holdings have no cost basis; the period needs a cash-only opening")
        return summary(ScoreStatus.UNSUPPORTED, [])

    gaps = []
    start = opening.cash
    realized = None
    if replay.final_cash is not None:
        realized = sum(
            (s.realized_pnl for s in replay.trade_scores if s.status == ScoreStatus.SCORED),
            Decimal(0),
        )
    if failed := statuses.count(ScoreStatus.FAILED):
        gaps.append(f"{failed} trade score(s) failed")

    replayed = None
    if replay.final_cash is None:
        gaps.append("the ledger was not replayed (see trade score reasons)")
    else:
        held: dict[str, Decimal] = {}
        for lot in replay.open_lots:
            held[lot.symbol] = held.get(lot.symbol, Decimal(0)) + lot.quantity
        replayed = (replay.final_cash, held)
        if outcome.final_account is None:
            gaps.append("no final account snapshot")
        elif (final := (outcome.final_account.cash, _holdings(outcome.final_account))) != replayed:
            gaps.append(
                f"final account cash {final[0]} holdings {final[1]} differs from replay "
                f"cash {replayed[0]} holdings {replayed[1]}"
            )

    end = market_end = unrealized = drawdown = drawdown_fraction = None
    if not marks:
        gaps.append("no marks, so there is no end value")
    else:
        last = marks[-1]
        end, known_rule = _value(last, config)
        market_end = last.portfolio_value
        if not known_rule:
            gaps.append(f"unknown valuation rule {last.valuation_rule_version}")
        elif end != market_end:
            gaps.append(f"end_value {end} differs from market portfolio_value {market_end}")
        if evidence.orders and last.simulated_at < evidence.orders[-1].account.simulated_at:
            gaps.append(
                f"last mark at {last.simulated_at.isoformat()} is before the last order at "
                f"{evidence.orders[-1].account.simulated_at.isoformat()}"
            )
        if replayed is not None and (last.cash, _holdings(last)) != replayed:
            gaps.append(
                f"last mark cash {last.cash} holdings {_holdings(last)} differs from replay "
                f"cash {replayed[0]} holdings {replayed[1]}"
            )
        if replayed is not None:
            if any(lot.cost_basis is None for lot in replay.open_lots):
                notes.append("unrealized PnL unknown: an open lot has no cost basis")
            else:
                basis = sum((lot.cost_basis for lot in replay.open_lots), Decimal(0))
                unrealized = end - last.cash - basis
        if len(marks) < 2:
            notes.append("max drawdown needs at least 2 marks")
        else:
            drawdown, drawdown_fraction = _drawdown([_value(m, config)[0] for m in marks])

    if replayed is None:
        notes.append("PnL and return need a replayed ledger; see trade score reasons")
    net = None if realized is None or unrealized is None else realized + unrealized
    if net is not None and end is not None and start + net != end:
        gaps.append(f"start_value {start} + net_pnl {net} != end_value {end}")
    period_return = None
    if not start:
        notes.append("return undefined: start_value is 0")
    elif net is not None:
        period_return = net / start

    return summary(
        ScoreStatus.UNSUPPORTED if replayed is None else ScoreStatus.SCORED,
        gaps,
        start_value=start,
        end_value=end,
        market_end_value=market_end,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        fees=sum((f.fee for f in fills), Decimal(0)),
        net_pnl=net,
        period_return=period_return,
        excess_return_vs_cash=period_return,
        max_drawdown=drawdown,
        max_drawdown_fraction=drawdown_fraction,
    )

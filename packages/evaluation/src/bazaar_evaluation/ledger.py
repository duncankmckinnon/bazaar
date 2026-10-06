"""Replay a run's ledger with FIFO lots and score each order's realized PnL."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from uuid import UUID

from bazaar_protocol import (
    AccountSnapshot,
    ExactAmount,
    FilledOrder,
    OrderSide,
    PositiveAmount,
    RejectedOrder,
    Symbol,
)
from pydantic import AwareDatetime

from bazaar_evaluation._base import EvaluationModel
from bazaar_evaluation.config import CashRoundingRule, EvaluatorConfig
from bazaar_evaluation.inputs import RunEvidence
from bazaar_evaluation.results import Evidence, ScoreStatus, TradeScore


class OpenLot(EvaluationModel):
    """Shares still held at the end of replay. `cost_basis` includes the buy fee's share.

    Lots without a basis or opening order come from the opening account or a ledger resync.
    """

    symbol: Symbol
    quantity: PositiveAmount
    cost_basis: ExactAmount | None
    opened_by: UUID | None
    opened_at: AwareDatetime


class LedgerReplay(EvaluationModel):
    trade_scores: tuple[TradeScore, ...]
    open_lots: tuple[OpenLot, ...]


@dataclass
class _Lot:
    quantity: Decimal
    cost_basis: Decimal | None
    opened_by: UUID | None
    opened_at: datetime


class _Book:
    def __init__(self, account: AccountSnapshot, rule: CashRoundingRule) -> None:
        self.rule = rule
        self.cash = account.cash
        self.lots: dict[str, list[_Lot]] = {}
        self.resync(account)

    def holdings(self) -> dict[str, Decimal]:
        held = {s: sum((lot.quantity for lot in lots), Decimal(0)) for s, lots in self.lots.items()}
        return {s: q for s, q in held.items() if q}

    def cash_delta(self, amount: Decimal) -> Decimal:
        """Round one fill's cash movement under the run's execution rule."""
        quantum = self.rule.quantum
        if quantum is None:
            return amount
        mode = ROUND_HALF_EVEN if self.rule.rounding == "half_even" else ROUND_HALF_UP
        return (amount / quantum).quantize(Decimal(1), rounding=mode) * quantum

    def buy(self, fill: FilledOrder) -> None:
        cost = self.cash_delta(fill.quantity * fill.unit_price + fill.fee)
        self.cash -= cost
        self.lots.setdefault(fill.symbol, []).append(
            _Lot(fill.quantity, cost, fill.order_id, fill.executed_at)
        )

    def sell(self, fill: FilledOrder) -> tuple[Decimal, Decimal | None, Decimal]:
        """Close lots FIFO; return (net proceeds, closed basis or None if unknown, uncovered)."""
        proceeds = self.cash_delta(fill.quantity * fill.unit_price - fill.fee)
        self.cash += proceeds
        lots = self.lots.get(fill.symbol, [])
        remaining = fill.quantity
        basis: Decimal | None = Decimal(0)
        while remaining and lots:
            lot = lots[0]
            closed = min(remaining, lot.quantity)
            if lot.cost_basis is None:
                basis = None
                piece = None
            elif closed == lot.quantity:
                piece = lot.cost_basis  # the whole remaining basis, so no rounding residue
            else:
                piece = lot.cost_basis * closed / lot.quantity
            if piece is not None:
                lot.cost_basis -= piece
                if basis is not None:
                    basis += piece
            lot.quantity -= closed
            remaining -= closed
            if not lot.quantity:
                lots.pop(0)
        return proceeds, basis, remaining

    def resync(self, account: AccountSnapshot) -> None:
        """Adopt the ledger's state; shares the replay cannot explain get an unknown basis."""
        self.cash = account.cash
        target = {h.symbol: h.quantity for h in account.holdings}
        for symbol in set(self.lots) | set(target):
            lots = self.lots.setdefault(symbol, [])
            excess = sum((lot.quantity for lot in lots), Decimal(0)) - target.get(symbol, 0)
            while excess > 0:
                lot = lots[0]
                dropped = min(excess, lot.quantity)
                if lot.cost_basis is not None:
                    lot.cost_basis -= lot.cost_basis * dropped / lot.quantity
                lot.quantity -= dropped
                excess -= dropped
                if not lot.quantity:
                    lots.pop(0)
            if excess < 0:
                lots.append(_Lot(-excess, None, None, account.simulated_at))
            if not lots:
                del self.lots[symbol]

    def mismatch(self, account: AccountSnapshot) -> str:
        expected = (self.cash, self.holdings())
        actual = (account.cash, {h.symbol: h.quantity for h in account.holdings})
        if expected == actual:
            return ""
        return (
            f"ledger mismatch: replay expects cash {expected[0]} holdings {expected[1]}, "
            f"ledger reports cash {actual[0]} holdings {actual[1]}"
        )

    def open_lots(self) -> tuple[OpenLot, ...]:
        return tuple(
            OpenLot(
                symbol=symbol,
                quantity=lot.quantity,
                cost_basis=lot.cost_basis,
                opened_by=lot.opened_by,
                opened_at=lot.opened_at,
            )
            for symbol, lots in sorted(self.lots.items())
            for lot in lots
        )


def _evidence(order: FilledOrder | RejectedOrder, reason: str = "") -> tuple[Evidence, ...]:
    if isinstance(order, RejectedOrder):
        return (Evidence(order_id=order.order_id, reason=reason),)
    return (
        Evidence(
            order_id=order.order_id,
            data_version=order.data_version,
            execution_rule_version=order.execution_rule_version,
            source=order.price_source,
            reason=reason,
        ),
    )


def replay_ledger(evidence: RunEvidence, config: EvaluatorConfig) -> LedgerReplay:
    """Score every order exactly once, in order. Failures are reported, never raised or dropped."""

    def score(order, status, reason="", **metrics) -> TradeScore:
        return TradeScore(
            order_id=order.order_id,
            status=status,
            evaluator_version=config.evaluator_version,
            evidence=_evidence(order, reason),
            **metrics,
        )

    if config.lot_method != "fifo":
        reason = f"lot_method {config.lot_method} needs lot ids, which fills do not carry"
        return _all_unsupported(evidence, reason, score)
    context = evidence.context
    rule = config.execution_rules.get(context.execution_rule_version)
    if rule is None:
        reason = f"unknown execution rule {context.execution_rule_version}"
        return _all_unsupported(evidence, reason, score)

    book = _Book(evidence.opening_account, rule)
    scores = []
    for order in evidence.orders:
        if isinstance(order, RejectedOrder):
            if drift := book.mismatch(order.account):
                book.resync(order.account)
                scores.append(score(order, ScoreStatus.FAILED, f"rejection snapshot {drift}"))
            else:
                reason = f"order rejected: {order.error.code}"
                scores.append(score(order, ScoreStatus.UNSUPPORTED, reason))
            continue
        problems = [
            f"fill {field} {getattr(order, field)} differs from context {getattr(context, field)}"
            for field in ("data_version", "execution_rule_version")
            if getattr(order, field) != getattr(context, field)
        ]
        if order.side == OrderSide.BUY:
            book.buy(order)
            uncovered, realized, closed = Decimal(0), Decimal(0), Decimal(0)
        else:
            proceeds, basis, uncovered = book.sell(order)
            closed = order.quantity
            realized = None if basis is None else proceeds - basis
        if mismatch := book.mismatch(order.account):
            problems.append(mismatch)
        if uncovered:
            problems.append(f"sold {uncovered} more shares than held")
        if problems:
            book.resync(order.account)
            scores.append(score(order, ScoreStatus.FAILED, "; ".join(problems), fee=order.fee))
        elif realized is None:
            reason = "cost basis unknown for shares from the opening account or a ledger resync"
            scores.append(score(order, ScoreStatus.UNSUPPORTED, reason, fee=order.fee))
        else:
            scores.append(
                score(
                    order,
                    ScoreStatus.SCORED,
                    realized_pnl=realized,
                    closed_quantity=closed,
                    fee=order.fee,
                )
            )
    return LedgerReplay(trade_scores=tuple(scores), open_lots=book.open_lots())


def _all_unsupported(evidence: RunEvidence, reason: str, score) -> LedgerReplay:
    trade_scores = tuple(score(o, ScoreStatus.UNSUPPORTED, reason) for o in evidence.orders)
    return LedgerReplay(trade_scores=trade_scores, open_lots=())

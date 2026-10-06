from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from uuid import UUID

import pytest
from bazaar_evaluation import (
    CashRoundingRule,
    EvaluatorConfig,
    OpenLot,
    RunEvidence,
    ScoreStatus,
    TradeScore,
    replay_ledger,
)
from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ExecutionErrorDetail,
    ExperimentContext,
    FilledOrder,
    Holding,
    RejectedOrder,
)
from pydantic import ValidationError

ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")
AGENT = UUID("00000000-0000-0000-0000-000000000002")
EXPERIMENT = UUID("00000000-0000-0000-0000-000000000003")
STRATEGY = UUID("00000000-0000-0000-0000-000000000004")
OPEN = datetime(2025, 7, 1, 13, 30, tzinfo=UTC)
EXACT = {"immediate-v1": CashRoundingRule(quantum=None)}
CONFIG = EvaluatorConfig(evaluator_version="evals-v1", execution_rules=EXACT)


def order_id(n):
    return UUID(int=1000 + n)


def at(n):
    return OPEN + timedelta(days=n)


def snapshot(when, cash, holdings=(), state_version=0):
    return AccountSnapshot(
        account_id=ACCOUNT,
        agent_id=AGENT,
        experiment_id=EXPERIMENT,
        strategy_version_id=STRATEGY,
        simulated_at=when,
        state_version=state_version,
        cash=cash,
        holdings=tuple(Holding(symbol=s, quantity=q) for s, q in holdings),
    )


class Run:
    """Builds a run whose post-fill snapshots carry hand-calculated cash and holdings."""

    def __init__(self, cash="10000", holdings=()):
        self.opening = snapshot(OPEN, cash, holdings)
        self.orders = []
        self.ids = count(1)

    def fill(
        self,
        side,
        quantity,
        price,
        cash,
        holdings,
        fee="0",
        symbol="AAPL",
        data_version="fixture-v1",
        execution_rule_version="immediate-v1",
    ):
        n = next(self.ids)
        self.orders.append(
            FilledOrder(
                order_id=order_id(n),
                client_order_id=UUID(int=n),
                symbol=symbol,
                side=side,
                quantity=quantity,
                unit_price=price,
                fee=fee,
                executed_at=at(n),
                price_observed_at=at(n),
                price_available_at=at(n),
                price_source="fixture",
                data_version=data_version,
                execution_rule_version=execution_rule_version,
                account=snapshot(at(n), cash, holdings, n),
            )
        )
        return self

    def reject(self, side, quantity, cash, holdings, symbol="AAPL"):
        n = next(self.ids)
        self.orders.append(
            RejectedOrder(
                order_id=order_id(n),
                client_order_id=UUID(int=n),
                symbol=symbol,
                side=side,
                quantity=quantity,
                rejected_at=at(n),
                error=ExecutionErrorDetail(code=ErrorCode.INSUFFICIENT_CASH, message="no cash"),
                account=snapshot(at(n), cash, holdings, n),
            )
        )
        return self

    def evidence(self):
        context = ExperimentContext(
            experiment_id=EXPERIMENT,
            agent_id=AGENT,
            account_id=ACCOUNT,
            strategy_version_id=STRATEGY,
            approval_id=UUID(int=99),
            simulated_at=OPEN,
            event_sequence=0,
            data_version="fixture-v1",
            execution_rule_version="immediate-v1",
        )
        return RunEvidence(context=context, opening_account=self.opening, orders=tuple(self.orders))


def scores(run, config=CONFIG):
    replay = replay_ledger(run.evidence(), config)
    assert [s.order_id for s in replay.trade_scores] == [o.order_id for o in run.orders]
    return replay


def test_profit_round_trip():
    # Buy 10 @ 200 = 2000 -> cash 8000. Sell 10 @ 220 = 2200 -> cash 10200.
    # Realized = 2200 - 2000 = 200.
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    run.fill("sell", "10", "220", "10200", [])
    buy, sell = scores(run).trade_scores

    assert buy.status == sell.status == ScoreStatus.SCORED
    assert (buy.realized_pnl, buy.closed_quantity) == (0, 0)
    assert (sell.realized_pnl, sell.closed_quantity) == (Decimal(200), Decimal(10))


def test_loss_round_trip_is_scored_not_dropped():
    # Buy 10 @ 200 -> cash 8000. Sell 10 @ 180 = 1800 -> cash 9800. Realized = 1800 - 2000 = -200.
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    run.fill("sell", "10", "180", "9800", [])
    _, sell = scores(run).trade_scores

    assert sell.status == ScoreStatus.SCORED
    assert sell.realized_pnl == Decimal(-200)


def test_fees_on_both_legs_are_each_counted_once():
    # Buy 10 @ 200 fee 5: cash 10000 - 2000 - 5 = 7995; lot basis 2005.
    # Sell 10 @ 220 fee 7: cash 7995 + 2200 - 7 = 10188.
    # Realized = (2200 - 7) - 2005 = 188, which equals the total cash change 10188 - 10000.
    run = Run().fill("buy", "10", "200", "7995", [("AAPL", "10")], fee="5")
    run.fill("sell", "10", "220", "10188", [], fee="7")
    replay = scores(run)
    buy, sell = replay.trade_scores

    assert (buy.fee, sell.fee) == (Decimal(5), Decimal(7))
    assert buy.realized_pnl == 0
    assert sell.realized_pnl == Decimal(188)
    assert sum(s.realized_pnl for s in replay.trade_scores) == Decimal(10188) - Decimal(10000)


def test_no_trade_gives_no_scores():
    replay = replay_ledger(Run().evidence(), CONFIG)

    assert replay.trade_scores == ()
    assert replay.open_lots == ()


def test_zero_capital_every_buy_rejected_is_unsupported():
    run = Run(cash="0").reject("buy", "1", "0", []).reject("buy", "5", "0", [], symbol="MSFT")
    replay = scores(run)

    assert [s.status for s in replay.trade_scores] == [ScoreStatus.UNSUPPORTED] * 2
    for score in replay.trade_scores:
        assert score.realized_pnl is None
        assert "insufficient_cash" in score.evidence[0].reason


def test_partial_close_consumes_fifo_lots_across_two_buys():
    # Buy 3 @ 100 fee 3: cash 10000 - 303 = 9697; lot A basis 303.
    # Buy 7 @ 110 fee 7: cash 9697 - 777 = 8920; lot B basis 777.
    # Sell 4 @ 120 fee 4: cash 8920 + 480 - 4 = 9396.
    #   FIFO closes all of A (303) and 1 of 7 from B (777 / 7 = 111): closed basis 414.
    #   Realized = (480 - 4) - 414 = 62.
    # Open remainder: 6 shares of B with basis 777 - 111 = 666.
    run = Run().fill("buy", "3", "100", "9697", [("AAPL", "3")], fee="3")
    run.fill("buy", "7", "110", "8920", [("AAPL", "10")], fee="7")
    run.fill("sell", "4", "120", "9396", [("AAPL", "6")], fee="4")
    replay = scores(run)
    sell = replay.trade_scores[2]

    assert sell.status == ScoreStatus.SCORED
    assert sell.closed_quantity == Decimal(4)
    assert sell.realized_pnl == Decimal(62)
    assert replay.open_lots == (
        OpenLot(
            symbol="AAPL",
            quantity="6",
            cost_basis="666",
            opened_by=order_id(2),
            opened_at=at(2),
        ),
    )


def test_open_position_buy_is_scored_and_lot_remains():
    # Buy 10 @ 200 -> cash 8000, lot basis 2000, never sold.
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    replay = scores(run)
    (buy,) = replay.trade_scores

    assert buy.status == ScoreStatus.SCORED
    assert (buy.realized_pnl, buy.closed_quantity) == (0, 0)
    assert replay.open_lots == (
        OpenLot(
            symbol="AAPL", quantity="10", cost_basis="2000", opened_by=order_id(1), opened_at=at(1)
        ),
    )


def test_rejected_order_mid_run_is_kept_and_does_not_disturb_replay():
    # Buy 10 @ 200 -> 8000. MSFT buy rejected, state unchanged. Sell 10 @ 210 -> 10100.
    # Realized = 2100 - 2000 = 100.
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    run.reject("buy", "1000", "8000", [("AAPL", "10")], symbol="MSFT")
    run.fill("sell", "10", "210", "10100", [])
    _, rejected, sell = scores(run).trade_scores

    assert rejected.status == ScoreStatus.UNSUPPORTED
    assert rejected.realized_pnl is None
    assert sell.status == ScoreStatus.SCORED
    assert sell.realized_pnl == Decimal(100)


def test_ledger_cash_mismatch_fails_that_fill_and_replay_continues():
    # Buy 10 @ 200 should leave 8000, but the ledger says 8001: failed with both values.
    # Replay resyncs to 8001. Sell 10 @ 220 -> 8001 + 2200 = 10201; realized 2200 - 2000 = 200.
    run = Run().fill("buy", "10", "200", "8001", [("AAPL", "10")])
    run.fill("sell", "10", "220", "10201", [])
    buy, sell = scores(run).trade_scores

    assert buy.status == ScoreStatus.FAILED
    assert buy.realized_pnl is None
    reason = buy.evidence[0].reason
    assert "8000" in reason and "8001" in reason
    assert sell.status == ScoreStatus.SCORED
    assert sell.realized_pnl == Decimal(200)


def test_ledger_holdings_mismatch_leaves_unknown_basis_shares_unsupported():
    # Buy 10 @ 200 but the ledger reports 11 shares: failed. Replay resyncs, and the extra share
    # has no known cost basis, so selling all 11 cannot claim a realized PnL.
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "11")])
    run.fill("sell", "11", "220", "10420", [])
    buy, sell = scores(run).trade_scores

    assert buy.status == ScoreStatus.FAILED
    assert "AAPL" in buy.evidence[0].reason
    assert sell.status == ScoreStatus.UNSUPPORTED
    assert "cost basis" in sell.evidence[0].reason


def test_opening_holdings_without_basis_make_their_sale_unsupported():
    # The opening account already holds 5 AAPL; we never saw what they cost.
    run = Run(cash="0", holdings=[("AAPL", "5")]).fill("sell", "5", "220", "1100", [])
    replay = scores(run)

    assert replay.trade_scores[0].status == ScoreStatus.UNSUPPORTED
    assert "cost basis" in replay.trade_scores[0].evidence[0].reason
    assert replay.open_lots == ()


def test_specific_lot_config_marks_every_order_unsupported():
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    run.fill("sell", "10", "220", "10200", [])
    config = EvaluatorConfig(
        evaluator_version="evals-v1", lot_method="specific_lot", execution_rules=EXACT
    )
    replay = scores(run, config)

    assert [s.status for s in replay.trade_scores] == [ScoreStatus.UNSUPPORTED] * 2
    assert all("specific_lot" in s.evidence[0].reason for s in replay.trade_scores)


def test_every_score_links_order_and_version_evidence():
    run = Run().fill("buy", "10", "200", "8001", [("AAPL", "10")])
    run.reject("buy", "1000", "8001", [("AAPL", "10")], symbol="MSFT")
    run.fill("sell", "10", "220", "10201", [])
    replay = scores(run)

    for order, score in zip(run.orders, replay.trade_scores, strict=True):
        assert score.evaluator_version == "evals-v1"
        assert score.evidence[0].order_id == order.order_id
        if order.status == "filled":
            assert score.evidence[0].data_version == "fixture-v1"
            assert score.evidence[0].execution_rule_version == "immediate-v1"
            assert score.evidence[0].source == "fixture"


def test_scored_result_requires_evidence():
    with pytest.raises(ValidationError, match="evidence"):
        TradeScore(
            order_id=order_id(1),
            symbol="AAPL",
            side="buy",
            status=ScoreStatus.SCORED,
            evaluator_version="v1",
        )


def test_ledger_that_allows_overselling_fails_the_sell():
    # Buy 5 @ 200 -> 9000. The ledger then fills a sell of 10 @ 220 (+2200 -> 11200) and reports
    # no holdings, so cash and holdings agree with replay but 5 sold shares were never held.
    run = Run().fill("buy", "5", "200", "9000", [("AAPL", "5")])
    run.fill("sell", "10", "220", "11200", [])
    _, sell = scores(run).trade_scores

    assert sell.status == ScoreStatus.FAILED
    assert "more shares than held" in sell.evidence[0].reason


@pytest.mark.parametrize(
    ("field", "value"),
    [("data_version", "fixture-v2"), ("execution_rule_version", "next-open-v1")],
)
def test_version_mismatch_fails_that_fill_and_later_fills_score(field, value):
    # Buy 10 @ 200 fee 1 -> 10000 - 2001 = 7999, under a version the context did not declare.
    # Sell 10 @ 220 -> 7999 + 2200 = 10199 under the declared versions.
    run = Run().fill("buy", "10", "200", "7999", [("AAPL", "10")], fee="1", **{field: value})
    run.fill("sell", "10", "220", "10199", [])
    buy, sell = scores(run).trade_scores

    assert buy.status == ScoreStatus.FAILED
    assert buy.fee == 1
    assert value in buy.evidence[0].reason and field in buy.evidence[0].reason
    assert sell.status == ScoreStatus.SCORED
    assert sell.realized_pnl == 199  # 2200 - (2000 + 1 buy fee)


def test_rejected_order_with_drifted_snapshot_fails_with_both_states():
    # Buy 10 @ 200 -> 8000, but the rejection then reports cash 7500: drift, so failed.
    # Replay adopts 7500. Sell 10 @ 220 -> 7500 + 2200 = 9700; realized 200.
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    run.reject("buy", "1000", "7500", [("AAPL", "10")], symbol="MSFT")
    run.fill("sell", "10", "220", "9700", [])
    _, rejected, sell = scores(run).trade_scores

    assert rejected.status == ScoreStatus.FAILED
    assert "8000" in rejected.evidence[0].reason and "7500" in rejected.evidence[0].reason
    assert rejected.fee is None
    assert sell.status == ScoreStatus.SCORED
    assert sell.realized_pnl == 200


def test_mismatch_and_oversell_failures_both_carry_the_fee():
    mismatch = Run().fill("buy", "10", "200", "7990", [("AAPL", "10")], fee="3")
    oversell = Run().fill("buy", "5", "200", "9000", [("AAPL", "5")])
    oversell.fill("sell", "10", "220", "11198", [], fee="2")

    assert scores(mismatch).trade_scores[0].fee == 3
    failed = scores(oversell).trade_scores[1]
    assert failed.status == ScoreStatus.FAILED
    assert failed.fee == 2


@pytest.mark.parametrize(
    ("rule", "status"),
    [
        (CashRoundingRule(quantum=None), ScoreStatus.FAILED),
        (CashRoundingRule(quantum="0.01", rounding="half_even"), ScoreStatus.SCORED),
        (CashRoundingRule(quantum="0.01", rounding="half_up"), ScoreStatus.FAILED),
    ],
)
def test_execution_rule_rounds_each_fill_cash_delta(rule, status):
    # Buy 0.5 @ 200.01 = 100.005. Half-even to the cent gives 100.00 -> cash 9900.00, which the
    # ledger reports. Exact (9899.995) and half-up (100.01 -> 9899.99) both disagree.
    run = Run().fill("buy", "0.5", "200.01", "9900.00", [("AAPL", "0.5")])
    config = EvaluatorConfig(evaluator_version="v1", execution_rules={"immediate-v1": rule})
    (buy,) = scores(run, config).trade_scores

    assert buy.status == status


def test_rounded_cost_is_the_lot_basis_so_realized_matches_cash():
    # Half-even cents. Buy 0.5 @ 200.01: cost 100.005 -> 100.00, cash 9900.00, basis 100.00.
    # Sell 0.5 @ 210.03: proceeds 105.015 -> 105.02 (half-even: 1 is odd, rounds up),
    # cash 10005.02. Realized = 105.02 - 100.00 = 5.02 = cash change 10005.02 - 10000.
    run = Run().fill("buy", "0.5", "200.01", "9900.00", [("AAPL", "0.5")])
    run.fill("sell", "0.5", "210.03", "10005.02", [])
    rule = CashRoundingRule(quantum="0.01", rounding="half_even")
    config = EvaluatorConfig(evaluator_version="v1", execution_rules={"immediate-v1": rule})
    _, sell = scores(run, config).trade_scores

    assert sell.status == ScoreStatus.SCORED
    assert sell.realized_pnl == Decimal("5.02")


def test_unknown_execution_rule_makes_every_order_unsupported():
    run = Run().fill("buy", "10", "200", "8000", [("AAPL", "10")])
    run.reject("buy", "1000", "8000", [("AAPL", "10")], symbol="MSFT")
    config = EvaluatorConfig(evaluator_version="v1", execution_rules={})
    replay = scores(run, config)

    assert [s.status for s in replay.trade_scores] == [ScoreStatus.UNSUPPORTED] * 2
    assert all(
        s.evidence[0].reason == "unknown execution rule immediate-v1" for s in replay.trade_scores
    )

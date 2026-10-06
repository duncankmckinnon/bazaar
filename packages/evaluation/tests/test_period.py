from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from uuid import UUID

import pytest
from bazaar_evaluation import (
    CashRoundingRule,
    EvaluatorConfig,
    RunEvidence,
    RunOutcome,
    ScoreStatus,
    evaluate_run,
)
from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ExecutionErrorDetail,
    ExperimentContext,
    FilledOrder,
    Holding,
    MarkedHolding,
    PortfolioSnapshot,
    RejectedOrder,
)

ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")
AGENT = UUID("00000000-0000-0000-0000-000000000002")
EXPERIMENT = UUID("00000000-0000-0000-0000-000000000003")
STRATEGY = UUID("00000000-0000-0000-0000-000000000004")
APPROVAL = UUID("00000000-0000-0000-0000-00000000000a")
OPEN = datetime(2025, 7, 1, 13, 30, tzinfo=UTC)
CENTS = CashRoundingRule(quantum="0.01", rounding="half_even")
# exec-v1: fee 0, whole shares, notional rounded to 0.01 half-even per fill.
# value-v1: qty x mark rounded to 0.01 half-even per holding; portfolio = cash + their sum.
CONFIG = EvaluatorConfig(
    evaluator_version="evals-v1",
    execution_rules={"exec-v1": CENTS},
    valuation_rules={"value-v1": CENTS},
)


def order_at(n):
    return OPEN + timedelta(days=n, hours=1)


def close_at(n):
    return OPEN + timedelta(days=n, hours=6, minutes=30)


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


def mark(day, cash, holdings, portfolio_value, valuation_rule_version="value-v1"):
    when = close_at(day)
    return PortfolioSnapshot(
        account_id=ACCOUNT,
        experiment_id=EXPERIMENT,
        simulated_at=when,
        state_version=day,
        cash=cash,
        holdings=tuple(
            MarkedHolding(
                symbol=s, quantity=q, unit_mark=m, mark_observed_at=when, mark_available_at=when
            )
            for s, q, m in holdings
        ),
        portfolio_value=portfolio_value,
        valuation_rule_version=valuation_rule_version,
        source="synthetic",
        data_version="synthetic-v1",
    )


class Run:
    def __init__(self, cash="10000"):
        self.opening = snapshot(OPEN, cash)
        self.orders = []
        self.ids = count(1)

    def fill(self, day, side, quantity, price, cash, holdings, symbol="AAPL"):
        n = next(self.ids)
        when = order_at(day)
        self.orders.append(
            FilledOrder(
                order_id=UUID(int=1000 + n),
                client_order_id=UUID(int=n),
                symbol=symbol,
                side=side,
                quantity=quantity,
                unit_price=price,
                fee="0",
                executed_at=when,
                price_observed_at=when,
                price_available_at=when,
                price_source="synthetic",
                data_version="synthetic-v1",
                execution_rule_version="exec-v1",
                account=snapshot(when, cash, holdings, n),
            )
        )
        return self

    def reject(self, day, quantity, cash, symbol="AAPL"):
        n = next(self.ids)
        when = order_at(day)
        self.orders.append(
            RejectedOrder(
                order_id=UUID(int=1000 + n),
                client_order_id=UUID(int=n),
                symbol=symbol,
                side="buy",
                quantity=quantity,
                rejected_at=when,
                error=ExecutionErrorDetail(code=ErrorCode.INSUFFICIENT_CASH, message="no cash"),
                account=snapshot(when, cash, (), n),
            )
        )
        return self

    def evaluate(self, marks, final_cash, final_holdings=(), config=CONFIG, **outcome):
        context = ExperimentContext(
            experiment_id=EXPERIMENT,
            agent_id=AGENT,
            account_id=ACCOUNT,
            strategy_version_id=STRATEGY,
            approval_id=APPROVAL,
            simulated_at=OPEN,
            event_sequence=0,
            data_version="synthetic-v1",
            execution_rule_version="exec-v1",
        )
        evidence = RunEvidence(
            context=context, opening_account=self.opening, orders=tuple(self.orders)
        )
        last = marks[-1].simulated_at if marks else OPEN
        result = evaluate_run(
            evidence,
            RunOutcome(
                marks=tuple(marks),
                final_account=snapshot(last, final_cash, final_holdings, 99),
                **({"run_status": "completed"} | outcome),
            ),
            config,
        )
        assert [s.order_id for s in result.trade_scores] == [o.order_id for o in self.orders]
        return result


def reasons(period):
    return [e.reason for e in period.evidence if e.reason]


def round_trip_with_open_position():
    # Open 10000 cash.
    # Day 1: buy 10 AAPL @ 150 = 1500.00 -> cash 8500.
    # Day 2: buy 5 MSFT @ 400 = 2000.00 -> cash 6500.
    # Day 3: sell 10 AAPL @ 165 = 1650.00 -> cash 8150. Realized 1650 - 1500 = 150.
    run = Run().fill(1, "buy", "10", "150", "8500", [("AAPL", "10")])
    run.fill(2, "buy", "5", "400", "6500", [("AAPL", "10"), ("MSFT", "5")], symbol="MSFT")
    run.fill(3, "sell", "10", "165", "8150", [("MSFT", "5")])
    return run


def final_mark(portfolio_value="10200", valuation_rule_version="value-v1"):
    # Day 3 close: MSFT 5 x 410 = 2050.00; portfolio 8150 + 2050 = 10200.
    return mark(3, "8150", [("MSFT", "5", "410")], portfolio_value, valuation_rule_version)


def test_round_trip_and_open_position_reconcile_to_the_cent():
    result = round_trip_with_open_position().evaluate([final_mark()], "8150", [("MSFT", "5")])
    p = result.period

    # Unrealized 2050 - 2000 = 50. Net 150 + 50 = 200 = 10200 - 10000. Return 200 / 10000.
    assert p.status == ScoreStatus.SCORED
    assert (p.start_value, p.end_value) == (Decimal(10000), Decimal(10200))
    assert (p.realized_pnl, p.unrealized_pnl) == (Decimal(150), Decimal(50))
    assert (p.fees, p.net_pnl) == (Decimal(0), Decimal(200))
    assert p.period_return == Decimal("0.02")
    assert p.excess_return_vs_cash == Decimal("0.02")
    assert p.reconciled is True
    assert not [r for r in reasons(p) if r.startswith("not reconciled")]
    assert p.max_drawdown is None and any("drawdown" in r for r in reasons(p))
    denominators = {d.name: d.value for d in p.denominators}
    assert denominators == {
        "orders": 3,
        "fills": 3,
        "rejections": 0,
        "scored": 3,
        "failed": 0,
        "unsupported": 0,
        "marks": 1,
    }
    assert (result.approval_id, result.data_version) == (APPROVAL, "synthetic-v1")
    assert result.execution_rule_version == "exec-v1"
    assert result.evaluator_version == "evals-v1"


def test_rounded_prices_still_reconcile_exactly():
    # Buy 3 AAPL @ 101.3333 = 303.9999 -> 304.00; cash 10000 - 304.00 = 9696.00; basis 304.00.
    # Sell 2 @ 105.1255 = 210.251 -> 210.25; cash 9906.25.
    #   Closed basis 304 x 2/3 = 202.666...; realized 210.25 - 202.666... = 7.58333...
    # Close: 1 AAPL x 104.505 -> 104.50 (half-even); portfolio 9906.25 + 104.50 = 10010.75.
    #   Unrealized 104.50 - 101.333... = 3.16666...; realized + unrealized = 10.75 exactly.
    run = Run().fill(1, "buy", "3", "101.3333", "9696.00", [("AAPL", "3")])
    run.fill(2, "sell", "2", "105.1255", "9906.25", [("AAPL", "1")])
    marks = [mark(2, "9906.25", [("AAPL", "1", "104.505")], "10010.75")]
    p = run.evaluate(marks, "9906.25", [("AAPL", "1")]).period

    assert p.end_value == Decimal("10010.75")
    assert p.net_pnl == Decimal("10.75")
    assert p.start_value + p.realized_pnl + p.unrealized_pnl == p.end_value
    assert p.reconciled is True


def test_zero_capital_all_rejected_has_a_summary_without_return():
    run = Run(cash="0").reject(1, "1", "0").reject(2, "5", "0", symbol="MSFT")
    result = run.evaluate([mark(2, "0", [], "0")], "0")
    p = result.period

    assert [s.status for s in result.trade_scores] == [ScoreStatus.UNSUPPORTED] * 2
    assert (p.start_value, p.end_value, p.net_pnl) == (0, 0, 0)
    assert p.period_return is None and p.excess_return_vs_cash is None
    assert any("start_value is 0" in r for r in reasons(p))
    assert {d.name: d.value for d in p.denominators}["rejections"] == 2
    assert p.reconciled is True


def test_failed_run_keeps_fills_and_records_the_failure():
    result = round_trip_with_open_position().evaluate(
        [final_mark()],
        "8150",
        [("MSFT", "5")],
        run_status="failed",
        run_failure="decide() raised TimeoutError",
    )

    assert result.run_status == "failed"
    assert len(result.trade_scores) == 3
    assert result.period.net_pnl == Decimal(200)
    assert "run failed: decide() raised TimeoutError" in reasons(result.period)


def test_last_mark_before_last_order_is_not_reconciled():
    # Day 2 close (before the day 3 sell): cash 6500, AAPL 10 x 160, MSFT 5 x 405.
    early = mark(2, "6500", [("AAPL", "10", "160"), ("MSFT", "5", "405")], "10125")
    p = round_trip_with_open_position().evaluate([early], "8150", [("MSFT", "5")]).period

    assert p.reconciled is False
    assert any("before the last order" in r for r in reasons(p))


def test_final_account_that_disagrees_with_replay_is_not_reconciled():
    p = round_trip_with_open_position().evaluate([final_mark()], "8151", [("MSFT", "5")]).period

    assert p.reconciled is False
    assert any("final account" in r and "8151" in r for r in reasons(p))


def test_three_mark_drawdown():
    # Buy 100 AAPL @ 100 = 10000 -> cash 0. Closes at 100, 110, 99: values 10000, 11000, 9900.
    # Peak 11000, trough 9900: drawdown 1100, fraction 1100 / 11000 = 0.1.
    # Net 9900 - 10000 = -100, return -0.01.
    run = Run().fill(1, "buy", "100", "100", "0", [("AAPL", "100")])
    marks = [
        mark(1, "0", [("AAPL", "100", "100")], "10000"),
        mark(2, "0", [("AAPL", "100", "110")], "11000"),
        mark(3, "0", [("AAPL", "100", "99")], "9900"),
    ]
    p = run.evaluate(marks, "0", [("AAPL", "100")]).period

    assert (p.max_drawdown, p.max_drawdown_fraction) == (Decimal(1100), Decimal("0.1"))
    assert (p.net_pnl, p.period_return) == (Decimal(-100), Decimal("-0.01"))
    assert p.reconciled is True


def test_known_valuation_rule_with_mismatched_portfolio_value_is_not_reconciled():
    run = round_trip_with_open_position()
    p = run.evaluate([final_mark("10199.99")], "8150", [("MSFT", "5")]).period

    assert (p.end_value, p.market_end_value) == (Decimal(10200), Decimal("10199.99"))
    assert p.reconciled is False
    assert any("10199.99" in r and "10200" in r for r in reasons(p))


def test_unknown_valuation_rule_is_not_reconciled():
    run = round_trip_with_open_position()
    p = run.evaluate([final_mark(valuation_rule_version="value-v9")], "8150", [("MSFT", "5")])
    p = p.period

    assert p.end_value == Decimal(10200)  # unquantized: 8150 + 5 x 410
    assert p.reconciled is False
    assert "not reconciled: unknown valuation rule value-v9" in reasons(p)


def test_opening_holdings_make_the_summary_unsupported():
    run = Run()
    run.opening = snapshot(OPEN, "10000", [("AAPL", "1")])
    result = run.evaluate(
        [mark(1, "10000", [("AAPL", "1", "100")], "10100")], "10000", [("AAPL", "1")]
    )

    assert result.period.status == ScoreStatus.UNSUPPORTED
    assert any("opening holdings" in r for r in reasons(result.period))


def test_outcome_for_another_account_is_rejected():
    other = round_trip_with_open_position()
    bad_mark = final_mark().model_copy(update={"account_id": UUID(int=7)})
    with pytest.raises(ValueError, match="account"):
        other.evaluate([bad_mark], "8150", [("MSFT", "5")])

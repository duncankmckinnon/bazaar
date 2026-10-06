from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from bazaar_protocol import AccountSnapshot, ExperimentContext, PriceObservation
from bazaar_replay import BuyAndHold, CashOnly

ID = UUID("00000000-0000-0000-0000-000000000001")
OPEN = datetime(2025, 7, 1, 13, 30, tzinfo=UTC)
LATER = datetime(2025, 7, 2, 13, 30, tzinfo=UTC)
PRICES = {"AAA": "100.00", "BBB": "333.33", "CCC": "47.50"}


class FakePrices:
    def __init__(self):
        self.calls = []

    def __call__(self, symbol, cutoff):
        self.calls.append((symbol, cutoff))
        return PriceObservation(observed_at=OPEN, available_at=OPEN, price=PRICES[symbol])


def ctx(at=OPEN, experiment_id=ID):
    return ExperimentContext(
        experiment_id=experiment_id,
        agent_id=ID,
        account_id=ID,
        strategy_version_id=ID,
        approval_id=ID,
        simulated_at=at,
        event_sequence=0,
        data_version="fixture-v1",
        execution_rule_version="immediate-v1",
    )


def account(at=OPEN, cash="10000"):
    return AccountSnapshot(
        account_id=ID,
        agent_id=ID,
        experiment_id=ID,
        strategy_version_id=ID,
        simulated_at=at,
        state_version=0,
        cash=cash,
    )


async def test_cash_only_never_orders():
    policy = CashOnly()

    assert await policy.decide(ctx(), account()) == ()
    assert await policy.decide(ctx(LATER), account(LATER)) == ()


async def test_buy_and_hold_sizes_equal_weight_whole_shares_from_remaining_cash():
    orders = await BuyAndHold(["AAA", "BBB", "CCC"], FakePrices()).decide(ctx(), account())

    # AAA: 10000 // (3 * 100.00) = 33, leaving 6700.00
    # BBB: 6700.00 // (2 * 333.33) = 10, leaving 3366.70
    # CCC: 3366.70 // 47.50 = 70, leaving 41.70
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [
        ("AAA", "buy", Decimal(33)),
        ("BBB", "buy", Decimal(10)),
        ("CCC", "buy", Decimal(70)),
    ]
    spent = sum(o.quantity * Decimal(PRICES[o.symbol]) for o in orders)
    assert spent == Decimal("9958.30")
    assert spent <= Decimal(10000)


async def test_buy_and_hold_skips_symbols_it_cannot_afford():
    orders = await BuyAndHold(["AAA", "BBB"], FakePrices()).decide(ctx(), account(cash="500"))

    # AAA: 500 // 200.00 = 2, leaving 300.00; BBB: 300.00 // 333.33 = 0, skipped.
    assert [(o.symbol, o.quantity) for o in orders] == [("AAA", Decimal(2))]


async def test_buy_and_hold_holds_after_first_decision():
    policy = BuyAndHold(["AAA", "BBB", "CCC"], FakePrices())

    assert await policy.decide(ctx(), account())
    assert await policy.decide(ctx(LATER), account(LATER)) == ()


async def test_buy_and_hold_reads_prices_only_at_the_decision_time():
    prices = FakePrices()
    policy = BuyAndHold(["AAA", "BBB", "CCC"], prices)

    await policy.decide(ctx(), account())
    await policy.decide(ctx(LATER), account(LATER))

    assert prices.calls == [("AAA", OPEN), ("BBB", OPEN), ("CCC", OPEN)]


async def test_client_order_ids_are_deterministic_per_experiment_and_symbol():
    first = await BuyAndHold(["AAA", "BBB"], FakePrices()).decide(ctx(), account())
    again = await BuyAndHold(["AAA", "BBB"], FakePrices()).decide(ctx(), account())
    other = UUID("00000000-0000-0000-0000-000000000002")
    elsewhere = await BuyAndHold(["AAA", "BBB"], FakePrices()).decide(
        ctx(experiment_id=other), account()
    )

    assert [o.client_order_id for o in first] == [o.client_order_id for o in again]
    assert len({o.client_order_id for o in first}) == 2
    assert {o.client_order_id for o in first}.isdisjoint(o.client_order_id for o in elsewhere)

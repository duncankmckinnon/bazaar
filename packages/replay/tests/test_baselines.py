from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from bazaar_protocol import AccountSnapshot, ExperimentContext, PriceObservation
from bazaar_replay import BuyAndHold, CashOnly, PriceAt

ID = UUID("00000000-0000-0000-0000-000000000001")
OPEN = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)
LATER = datetime(2026, 2, 13, 14, 30, tzinfo=UTC)
PRICES = {"AAPL": "100.00", "MSFT": "333.33", "KO": "47.50"}


class MissingPrice(LookupError):
    """Like the runner's MissingPrice (code data_unavailable)."""


class FakePrices:
    def __init__(self, available_at=OPEN, fail_once=None):
        self.calls = []
        self.available_at = available_at
        self.fail_once = fail_once

    async def __call__(self, symbol: str, cutoff: datetime) -> PriceObservation:
        self.calls.append((symbol, cutoff))
        if symbol == self.fail_once:
            self.fail_once = None
            raise MissingPrice(f"no {symbol} observation yet")
        return PriceObservation(
            observed_at=OPEN, available_at=self.available_at, price=PRICES[symbol]
        )


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

    assert await policy(ctx(), account()) == ()
    assert await policy(ctx(LATER), account(LATER)) == ()


async def test_buy_and_hold_sizes_equal_weight_whole_shares_from_remaining_cash():
    orders = await BuyAndHold(["AAPL", "MSFT", "KO"], FakePrices())(ctx(), account())

    # AAPL: 10000 // (3 * 100.00) = 33, leaving 6700.00
    # MSFT: 6700.00 // (2 * 333.33) = 10, leaving 3366.70
    # KO: 3366.70 // 47.50 = 70, leaving 41.70
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [
        ("AAPL", "buy", Decimal(33)),
        ("MSFT", "buy", Decimal(10)),
        ("KO", "buy", Decimal(70)),
    ]
    spent = sum(o.quantity * Decimal(PRICES[o.symbol]) for o in orders)
    assert spent == Decimal("9958.30")
    assert spent <= Decimal(10000)


async def test_buy_and_hold_skips_symbols_it_cannot_afford():
    orders = await BuyAndHold(["AAPL", "MSFT"], FakePrices())(ctx(), account(cash="500"))

    # AAPL: 500 // 200.00 = 2, leaving 300.00; MSFT: 300.00 // 333.33 = 0, skipped.
    assert [(o.symbol, o.quantity) for o in orders] == [("AAPL", Decimal(2))]


async def test_buy_and_hold_holds_after_first_decision():
    policy = BuyAndHold(["AAPL", "MSFT", "KO"], FakePrices())

    assert await policy(ctx(), account())
    assert await policy(ctx(LATER), account(LATER)) == ()


async def test_buy_and_hold_reads_prices_only_at_the_decision_time():
    prices = FakePrices()
    policy = BuyAndHold(["AAPL", "MSFT", "KO"], prices)

    await policy(ctx(), account())
    await policy(ctx(LATER), account(LATER))

    assert prices.calls == [("AAPL", OPEN), ("MSFT", OPEN), ("KO", OPEN)]


async def test_client_order_ids_are_deterministic_per_experiment_and_symbol():
    first = await BuyAndHold(["AAPL", "MSFT"], FakePrices())(ctx(), account())
    again = await BuyAndHold(["AAPL", "MSFT"], FakePrices())(ctx(), account())
    other = UUID("00000000-0000-0000-0000-000000000002")
    elsewhere = await BuyAndHold(["AAPL", "MSFT"], FakePrices())(
        ctx(experiment_id=other), account()
    )

    assert [o.client_order_id for o in first] == [o.client_order_id for o in again]
    assert len({o.client_order_id for o in first}) == 2
    assert {o.client_order_id for o in first}.isdisjoint(o.client_order_id for o in elsewhere)


def test_async_fake_satisfies_price_at():
    price_at: PriceAt = FakePrices()

    assert callable(price_at)


async def test_buy_and_hold_retries_after_a_lookup_fails_mid_basket():
    prices = FakePrices(fail_once="MSFT")
    policy = BuyAndHold(["AAPL", "MSFT", "KO"], prices)

    with pytest.raises(MissingPrice):
        await policy(ctx(), account())
    orders = await policy(ctx(), account())

    assert [symbol for symbol, _ in prices.calls] == ["AAPL", "MSFT", "AAPL", "MSFT", "KO"]

    assert [(o.symbol, o.quantity) for o in orders] == [
        ("AAPL", Decimal(33)),
        ("MSFT", Decimal(10)),
        ("KO", Decimal(70)),
    ]


async def test_buy_and_hold_rejects_a_price_from_the_future():
    policy = BuyAndHold(["AAPL"], FakePrices(available_at=datetime(2026, 2, 2, 20, tzinfo=UTC)))

    with pytest.raises(ValueError, match="not available"):
        await policy(ctx(), account())

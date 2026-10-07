from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ExperimentContext,
    OrderRequest,
    OrderSide,
    PriceObservation,
)
from bazaar_runner.market import MarketError, MissingPrice
from bazaar_runner.policy import Decision, DecisionPolicy, PriceAt

ID = UUID("00000000-0000-0000-0000-000000000001")
AT = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)
PREVIOUS_CLOSE = datetime(2026, 1, 30, 21, 0, tzinfo=UTC)


class BuyBelow:
    """A policy that reads as-of prices through an injected PriceAt, not through its arguments."""

    def __init__(self, price_at: PriceAt, limit: Decimal) -> None:
        self.price_at = price_at
        self.limit = limit

    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision:
        observation = await self.price_at("AAPL", ctx.simulated_at)
        if observation.price >= self.limit:
            return ()
        return (OrderRequest(client_order_id=ID, symbol="AAPL", side=OrderSide.BUY, quantity=1),)


async def fixed_price(symbol: str, cutoff: datetime) -> PriceObservation:
    return PriceObservation(observed_at=PREVIOUS_CLOSE, available_at=PREVIOUS_CLOSE, price="100")


CTX = ExperimentContext(
    experiment_id=ID,
    agent_id=ID,
    account_id=ID,
    strategy_version_id=ID,
    approval_id=ID,
    simulated_at=AT,
    event_sequence=0,
    data_version="synthetic-v1",
    execution_rule_version="exec-v1",
)
ACCOUNT = AccountSnapshot(
    account_id=ID,
    agent_id=ID,
    experiment_id=ID,
    strategy_version_id=ID,
    simulated_at=AT,
    state_version=0,
    cash="1000",
)


async def test_policy_gets_prices_from_its_injected_price_at():
    policy: DecisionPolicy = BuyBelow(fixed_price, limit=Decimal(150))
    (order,) = await policy(CTX, ACCOUNT)
    assert (order.symbol, order.side, order.quantity) == ("AAPL", OrderSide.BUY, 1)
    assert await BuyBelow(fixed_price, limit=Decimal(50))(CTX, ACCOUNT) == ()


def test_missing_price_is_a_typed_data_unavailable_market_error():
    error = MissingPrice("AAPL", AT)
    assert isinstance(error, MarketError)
    assert error.detail.code is ErrorCode.DATA_UNAVAILABLE
    assert (error.symbol, error.cutoff) == ("AAPL", AT)

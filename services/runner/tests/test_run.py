from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

import pytest
from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ErrorDetail,
    ExecutionErrorDetail,
    FilledOrder,
    Holding,
    MarkedHolding,
    OrderRequest,
    OrderSide,
    PortfolioSnapshot,
    PriceObservation,
    RejectedOrder,
)
from bazaar_runner.clock import ClockScript, TradingSession
from bazaar_runner.market import MarketError, MissingPrice
from bazaar_runner.run import RunResult, RunSpec, RunState, run_strategy


def session(day: date) -> TradingSession:
    # February 2026 is EST: 09:30-16:00 New York is 14:30-21:00 UTC.
    open_at = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
    return TradingSession(date=day, open_at=open_at, close_at=open_at + timedelta(hours=6.5))


# Demo fixture calendar: 2026-02-02..13, weekends skipped. Price data starts at the 2026-01-30 close.
SESSIONS = tuple(session(date(2026, 2, d)) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13))
CLOSES = (datetime(2026, 1, 30, 21, 0, tzinfo=UTC), *(s.close_at for s in SESSIONS))
# Synthetic: the close on trading day i (2026-01-30 is day 0) is base + i.
BASE = {"AAPL": 200, "MSFT": 400, "KO": 60}

SPEC = RunSpec(
    run_id=UUID("00000000-0000-0000-0000-0000000000a0"),
    experiment_id=UUID("00000000-0000-0000-0000-0000000000a1"),
    agent_id=UUID("00000000-0000-0000-0000-0000000000a2"),
    strategy_version_id=UUID("00000000-0000-0000-0000-0000000000a3"),
    approval_id=UUID("00000000-0000-0000-0000-0000000000a4"),
    data_version="synthetic-v1",
    execution_rule_version="exec-v1",
    starting_cash="10000",
    # Decision at each open (sees the previous close), mark at each close: D0 M1 D2 M3 ... D18 M19.
    script=ClockScript(sessions=SESSIONS, decision_offsets=(timedelta(0),)),
)


def conflict(message: str) -> MarketError:
    return MarketError(ErrorDetail(code=ErrorCode.CONFLICT, message=message))


class InMemoryMarket:
    """Test-only MarketPort: immediate fill or reject at the latest available close, zero fee."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.cutoff: datetime | None = None
        self.versions: tuple[str, str] | None = None
        self.accounts: dict[UUID, AccountSnapshot] = {}
        self.closed: set[UUID] = set()
        self._ids = 0

    def _id(self) -> UUID:
        self._ids += 1
        return UUID(int=self._ids)

    async def price_at(self, symbol, cutoff):
        available = [(i, at) for i, at in enumerate(CLOSES) if at <= cutoff]
        if symbol not in BASE or not available:
            raise MissingPrice(symbol, cutoff)
        day, close_at = available[-1]
        return PriceObservation(
            observed_at=close_at, available_at=close_at, price=Decimal(BASE[symbol] + day)
        )

    async def set_cutoff(self, experiment_id, cutoff, data_version, execution_rule_version):
        self.calls.append(("set_cutoff", cutoff))
        if self.versions is None:
            self.versions = (data_version, execution_rule_version)
        elif self.versions != (data_version, execution_rule_version):
            raise conflict("versions changed")
        elif cutoff < self.cutoff:
            raise conflict("cutoff moved backwards")
        self.cutoff = cutoff
        return cutoff

    async def create_account(
        self, experiment_id, agent_id, strategy_version_id, cash, holdings=(), *, request_id
    ):
        self.calls.append(("create_account", request_id))
        if self.cutoff is None:
            raise conflict("no experiment clock")
        snapshot = AccountSnapshot(
            account_id=self._id(),
            agent_id=agent_id,
            experiment_id=experiment_id,
            strategy_version_id=strategy_version_id,
            simulated_at=self.cutoff,
            state_version=0,
            cash=cash,
            holdings=tuple(holdings),
        )
        self.accounts[snapshot.account_id] = snapshot
        return snapshot

    def _now(self, account_id: UUID) -> AccountSnapshot:
        return self.accounts[account_id].model_copy(update={"simulated_at": self.cutoff})

    async def account(self, ctx):
        self.calls.append(("account", ctx.simulated_at))
        return self._now(ctx.account_id)

    async def submit(self, ctx, order: OrderRequest):
        self.calls.append(("submit", ctx.simulated_at, order.client_order_id))
        if ctx.account_id in self.closed:
            raise MarketError(
                ErrorDetail(code=ErrorCode.EXPERIMENT_NOT_RUNNING, message="account closed")
            )
        before = self._now(ctx.account_id)
        held = {h.symbol: h.quantity for h in before.holdings}
        try:
            price = await self.price_at(order.symbol, self.cutoff)
        except MissingPrice:
            price, error = None, ErrorCode.DATA_UNAVAILABLE
        else:
            sign = 1 if order.side is OrderSide.BUY else -1
            cash = before.cash - sign * order.quantity * price.price
            held[order.symbol] = held.get(order.symbol, Decimal(0)) + sign * order.quantity
            error = (
                ErrorCode.INSUFFICIENT_CASH
                if cash < 0
                else ErrorCode.INSUFFICIENT_HOLDINGS
                if held[order.symbol] < 0
                else None
            )
        common = {
            "order_id": self._id(),
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
        }
        if error is not None:
            return RejectedOrder(
                **common,
                rejected_at=self.cutoff,
                error=ExecutionErrorDetail(code=error, message=error.value),
                account=before,
            )
        after = before.model_copy(
            update={
                "cash": cash,
                "holdings": tuple(Holding(symbol=s, quantity=q) for s, q in held.items() if q),
                "state_version": before.state_version + 1,
            }
        )
        self.accounts[ctx.account_id] = after
        return FilledOrder(
            **common,
            unit_price=price.price,
            fee="0",
            executed_at=self.cutoff,
            price_observed_at=price.observed_at,
            price_available_at=price.available_at,
            price_source="fixture",
            data_version=ctx.data_version,
            execution_rule_version=ctx.execution_rule_version,
            account=after,
        )

    async def portfolio(self, ctx):
        self.calls.append(("portfolio", ctx.simulated_at))
        # Like the real market: no mark history, only the current cutoff.
        if ctx.simulated_at != self.cutoff:
            raise conflict("portfolio is only available at the current cutoff")
        account = self._now(ctx.account_id)
        holdings = []
        for h in account.holdings:
            mark = await self.price_at(h.symbol, self.cutoff)
            holdings.append(
                MarkedHolding(
                    symbol=h.symbol,
                    quantity=h.quantity,
                    unit_mark=mark.price,
                    mark_observed_at=mark.observed_at,
                    mark_available_at=mark.available_at,
                )
            )
        return PortfolioSnapshot(
            account_id=account.account_id,
            experiment_id=account.experiment_id,
            simulated_at=self.cutoff,
            state_version=account.state_version,
            cash=account.cash,
            holdings=tuple(holdings),
            portfolio_value=account.cash + sum(h.quantity * h.unit_mark for h in holdings),
            valuation_rule_version="fixture-close-v1",
            source="fixture",
            data_version=SPEC.data_version,
        )

    async def close_account(self, experiment_id, account_id):
        self.calls.append(("close_account", account_id))
        self.closed.add(account_id)
        return self._now(account_id)


def order(side: OrderSide, symbol: str, quantity: int, n: int) -> OrderRequest:
    return OrderRequest(
        client_order_id=uuid5(SPEC.run_id, str(n)), symbol=symbol, side=side, quantity=quantity
    )


# Whole shares only. Prices are the previous close, since each decision is at the open.
SCRIPTED = {
    # 2026-02-02, sees the 01-30 close: buy 10 AAPL at 200 for 2000.
    0: (order(OrderSide.BUY, "AAPL", 10, 0),),
    # 2026-02-03: an unaffordable MSFT buy is rejected, then buy 20 KO at 61 for 1220.
    2: (order(OrderSide.BUY, "MSFT", 1000, 1), order(OrderSide.BUY, "KO", 20, 2)),
    # 2026-02-04: sell 4 AAPL at 202 for 808.
    4: (order(OrderSide.SELL, "AAPL", 4, 3),),
}


def scripted_policy(seen: list):
    async def decide(ctx, account):
        seen.append((ctx, account))
        return SCRIPTED.get(ctx.event_sequence, ())

    return decide


def holdings(result: RunResult) -> dict[str, Decimal]:
    return {h.symbol: h.quantity for h in result.account.holdings}


async def test_scripted_run_completes_end_to_end():
    market, seen = InMemoryMarket(), []
    result = await run_strategy(SPEC, market, scripted_policy(seen))

    assert result.state is RunState.COMPLETED and result.failure is None
    assert result.account.cash == Decimal(7588)
    assert holdings(result) == {"AAPL": Decimal(6), "KO": Decimal(20)}
    assert [(o.event_sequence, o.order_index, o.result.status) for o in result.orders] == [
        (0, 0, "filled"),
        (2, 0, "rejected"),
        (2, 1, "filled"),
        (4, 0, "filled"),
    ]
    assert [o.result.unit_price for o in result.orders if o.result.status == "filled"] == [
        Decimal(200),
        Decimal(61),
        Decimal(202),
    ]
    assert [ctx.event_sequence for ctx, _ in seen] == list(range(0, 20, 2))
    assert [m.event_sequence for m in result.marks] == list(range(1, 20, 2))
    values = [m.portfolio.portfolio_value for m in result.marks]
    # 02-02 close: 8000 + 10*201. 02-03 close: 6780 + 10*202 + 20*62. Last: 7588 + 6*210 + 20*70.
    assert (values[0], values[1], values[-1]) == (Decimal(10010), Decimal(10040), Decimal(10248))
    assert RunResult.model_validate_json(result.model_dump_json()) == result


async def test_market_calls_follow_the_clock():
    market = InMemoryMarket()
    result = await run_strategy(SPEC, market, scripted_policy([]))

    kinds = [call[0] for call in market.calls]
    assert kinds[:2] == ["set_cutoff", "create_account"]
    assert market.calls[0][1] == SESSIONS[0].open_at
    # Every DECISION and MARK event moves the cutoff first; a mark reads only the current cutoff.
    cutoffs = [call[1] for call in market.calls if call[0] == "set_cutoff"][1:]
    assert cutoffs == [t for s in SESSIONS for t in (s.open_at, s.close_at)]
    for i, kind in enumerate(kinds):
        if kind in ("account", "portfolio"):
            assert kinds[i - 1] == "set_cutoff" and market.calls[i - 1][1] == market.calls[i][1]
    assert kinds[-1] == "close_account"
    assert result.account.account_id in market.closed


async def test_decisions_never_see_a_later_cutoff_account_or_price():
    market, seen = InMemoryMarket(), []
    scripted = scripted_policy(seen)

    async def decide(ctx, account):
        assert market.cutoff == ctx.simulated_at
        assert account.simulated_at <= ctx.simulated_at
        price = await market.price_at("AAPL", ctx.simulated_at)
        assert price.available_at < ctx.simulated_at
        return await scripted(ctx, account)

    result = await run_strategy(SPEC, market, decide)
    assert result.state is RunState.COMPLETED


async def test_rejected_order_is_recorded_and_the_run_continues():
    result = await run_strategy(SPEC, InMemoryMarket(), scripted_policy([]))
    rejected = [o.result for o in result.orders if o.result.status == "rejected"]
    assert [r.error.code for r in rejected] == [ErrorCode.INSUFFICIENT_CASH]
    assert rejected[0].account.cash == Decimal(8000)
    assert result.state is RunState.COMPLETED
    assert len(result.marks) == len(SESSIONS)


async def test_decide_raising_fails_the_run_and_keeps_its_fills():
    market, seen = InMemoryMarket(), []
    scripted = scripted_policy(seen)

    async def decide(ctx, account):
        if ctx.event_sequence == 6:
            raise RuntimeError("policy blew up")
        return await scripted(ctx, account)

    result = await run_strategy(SPEC, market, decide)

    assert result.state is RunState.FAILED
    assert result.failure == "RuntimeError: policy blew up"
    assert [o.event_sequence for o in result.orders] == [0, 2, 2, 4]
    assert [m.event_sequence for m in result.marks] == [1, 3, 5]
    # The losses and holdings stay as settled; nothing is rolled back.
    assert result.account.cash == Decimal(7588)
    assert holdings(result) == {"AAPL": Decimal(6), "KO": Decimal(20)}
    assert result.account.account_id in market.closed
    assert [c[0] for c in market.calls].count("set_cutoff") == 1 + 7


@pytest.mark.parametrize("fail_on", ["set_cutoff", "create_account"])
async def test_market_error_before_any_account_fails_the_run(fail_on):
    market = InMemoryMarket()

    async def refuse(*args, **kwargs):
        raise MarketError(ErrorDetail(code=ErrorCode.FORBIDDEN, message="no grant"))

    setattr(market, fail_on, refuse)
    result = await run_strategy(SPEC, market, scripted_policy([]))
    assert result.state is RunState.FAILED
    assert result.account is None and result.orders == () and result.marks == ()
    assert result.failure == "MarketError: forbidden: no grant"
    assert "close_account" not in [c[0] for c in market.calls]


async def test_close_failure_is_recorded_and_the_run_is_failed():
    market = InMemoryMarket()

    async def broken_close(*args):
        raise MarketError(ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="down"))

    market.close_account = broken_close
    result = await run_strategy(SPEC, market, scripted_policy([]))
    assert result.state is RunState.FAILED
    assert result.failure == (
        "close_account failed, account left open: MarketError: internal_error: down"
    )
    assert result.account.cash == Decimal(7588)

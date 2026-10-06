"""Test-only market: the demo fixture calendar and an in-memory MarketPort."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

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
from bazaar_runner.market import FutureData, MarketError, MissingPrice
from bazaar_runner.run import RunSpec

DATA_VERSION = "synthetic-v1"


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
    data_version=DATA_VERSION,
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
        if self.cutoff is not None and cutoff > self.cutoff:
            raise FutureData(f"{cutoff.isoformat()} is after the experiment cutoff")
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
            data_version=DATA_VERSION,
        )

    async def close_account(self, experiment_id, account_id):
        self.calls.append(("close_account", account_id))
        self.closed.add(account_id)
        return self._now(account_id)

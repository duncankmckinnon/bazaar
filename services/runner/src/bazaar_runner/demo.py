"""The AI Engineer NYC demo: a scripted agent and the baselines over one bundle's sessions."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from bazaar_protocol import AccountSnapshot, ExperimentContext, OrderRequest, OrderSide

from bazaar_runner.bundle import Bundle
from bazaar_runner.market import MarketPort
from bazaar_runner.policy import Decision, DecisionPolicy, PriceAt
from bazaar_runner.record import Evaluate, RunRecord, record_run
from bazaar_runner.run import DecisionStep, OrderRecord, RunSpec, StepOutcome, submit_order

AGENT_FIXTURE_REF = "agent-fixture-v1"
CASH_ONLY_REF = "baseline-cash-only"
# The S&P 500 demo (spec v5): the benchmark held from day 1, and momentum over the universe.
BUY_AND_HOLD_SPY_REF = "baseline-buy-and-hold-spy"
MOMENTUM_V2_REF = "scripted-momentum-v2"
# The 12-stock names, kept so older records stay distinguishable: momentum v1 reads one price
# per symbol, and the equal-weight basket is bazaar_replay.baselines.BuyAndHold.
MOMENTUM_REF = "scripted-momentum-v1"
BUY_AND_HOLD_REF = "baseline-buy-and-hold"
# Momentum v2: the day's top 1d movers, at most this many new holdings per decision.
MOMENTUM_SCREEN_ROWS = 5
MOMENTUM_NEW_PER_DAY = 2
MOMENTUM_FRACTION = Decimal("0.3")
# Agent and strategy ids are derived from the policy name until the demo goes through the registry.
_IDS = uuid5(NAMESPACE_URL, "https://github.com/duncankmckinnon/bazaar/runner")

# Called once per launch with the run's bundle (its universe and sessions) and that launch's own
# price lookup.
PolicyFactory = Callable[[Bundle, PriceAt], DecisionPolicy | DecisionStep]


class ScriptedMomentum:
    """Buy after a rise since the previous decision; sell the whole holding after a fall.

    Deterministic and whole-share. Reads only as-of prices at the decision time. One instance
    per run, since it remembers the last price it saw.
    """

    def __init__(
        self, symbols: Sequence[str], prices: PriceAt, fraction: Decimal = Decimal("0.3")
    ) -> None:
        self.symbols = tuple(symbols)
        self.prices = prices
        self.fraction = fraction
        self.last: dict[str, Decimal] = {}

    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision:
        held = {h.symbol: h.quantity for h in account.holdings}
        cash = account.cash
        orders = []
        for symbol in self.symbols:
            price = (await self.prices(symbol, ctx.simulated_at)).price
            previous, self.last[symbol] = self.last.get(symbol), price
            if previous is None:
                continue
            if price > previous and symbol not in held:
                side, quantity = OrderSide.BUY, (cash * self.fraction) // price
                cash -= quantity * price
            elif price < previous and symbol in held:
                side, quantity = OrderSide.SELL, held[symbol]
            else:
                continue
            if quantity:
                orders.append(
                    OrderRequest(
                        client_order_id=uuid5(ctx.experiment_id, f"{ctx.event_sequence}:{symbol}"),
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                    )
                )
        return tuple(orders)


class BuyAndHoldBenchmark:
    """At the first decision, buy floor(cash / close) whole shares of the bundle's benchmark,
    then hold. Zero fees (exec-v1). One instance per run."""

    def __init__(self, benchmark: str | None, prices: PriceAt) -> None:
        if benchmark is None:
            raise ValueError("this bundle has no benchmark to buy and hold")
        self.benchmark = benchmark
        self.prices = prices
        self.bought = False

    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision:
        if self.bought:
            return ()
        price = (await self.prices(self.benchmark, ctx.simulated_at)).price
        quantity = account.cash // price
        # Only a completed decision counts; a failed one is retried with the same order id.
        self.bought = True
        if not quantity:
            return ()
        order = OrderRequest(
            client_order_id=uuid5(ctx.experiment_id, f"{ctx.event_sequence}:{self.benchmark}"),
            symbol=self.benchmark,
            side=OrderSide.BUY,
            quantity=quantity,
        )
        return (order,)


class ScriptedMomentumV2(DecisionStep):
    """Momentum over a whole universe with one screen read per decision, never a price read
    per stock.

    Each decision: sell every holding whose 1d change is negative (quotes for the holdings),
    then buy the screen's top 1d movers not already held, in the market's order (ties by
    symbol), at most MOMENTUM_NEW_PER_DAY of them, each with MOMENTUM_FRACTION of the cash left.
    Only universe members are bought; a null change is skipped; whole shares; buys spend only
    the cash held before this decision's sells, so cash never goes negative."""

    def __init__(self, universe: Sequence[str]) -> None:
        self.universe = frozenset(universe)

    async def decide(
        self,
        ctx: ExperimentContext,
        account: AccountSnapshot,
        market: MarketPort,
        orders: list[OrderRecord],
    ) -> StepOutcome:
        from bazaar_agent.market_view import QUOTES_PER_CALL, QuotesRequest, ScreenRequest

        held = {h.symbol: h.quantity for h in account.holdings}
        screen = await market.screen(
            ctx, ScreenRequest(sort="pct_change_1d", direction="desc", limit=MOMENTUM_SCREEN_ROWS)
        )
        falling = []
        symbols = sorted(held)
        for start in range(0, len(symbols), QUOTES_PER_CALL):
            chunk = tuple(symbols[start : start + QUOTES_PER_CALL])
            quotes = await market.quotes(ctx, QuotesRequest(symbols=chunk))
            falling += [
                q.symbol
                for q in quotes.quotes
                if q.pct_change_1d is not None and q.pct_change_1d < 0
            ]
        decision: list[OrderRequest] = [
            self._order(ctx, symbol, OrderSide.SELL, held[symbol]) for symbol in sorted(falling)
        ]
        cash, bought = account.cash, 0
        for row in screen.rows:
            if bought == MOMENTUM_NEW_PER_DAY:
                break
            if (
                row.symbol not in self.universe
                or row.symbol in held
                or row.close is None
                or row.pct_change_1d is None
                or row.pct_change_1d <= 0
            ):
                continue
            quantity = (cash * MOMENTUM_FRACTION) // row.close
            if quantity:
                decision.append(self._order(ctx, row.symbol, OrderSide.BUY, quantity))
                cash -= quantity * row.close
                bought += 1
        for index, order in enumerate(decision):
            await submit_order(market, ctx, index, order, orders)
        return StepOutcome(
            attributes={"screened": len(screen.rows), "sells": len(falling), "buys": bought}
        )

    @staticmethod
    def _order(ctx: ExperimentContext, symbol: str, side: OrderSide, quantity) -> OrderRequest:
        return OrderRequest(
            client_order_id=uuid5(ctx.experiment_id, f"{ctx.event_sequence}:{symbol}"),
            symbol=symbol,
            side=side,
            quantity=quantity,
        )


@dataclass(frozen=True)
class Launch:
    policy_ref: str
    experiment_id: UUID
    approval_id: UUID


def demo_spec(
    launch: Launch, *, bundle: Bundle, execution_rule_version: str, starting_cash: Decimal
) -> RunSpec:
    return RunSpec(
        # One run per experiment, so a relaunch reuses the market's idempotent account request.
        run_id=uuid5(launch.experiment_id, "run"),
        experiment_id=launch.experiment_id,
        agent_id=uuid5(_IDS, f"agent:{launch.policy_ref}"),
        strategy_version_id=uuid5(_IDS, f"strategy:{launch.policy_ref}"),
        approval_id=launch.approval_id,
        data_version=bundle.bundle_id,
        execution_rule_version=execution_rule_version,
        starting_cash=starting_cash,
        script=bundle.script,
    )


class DuplicateLaunch(ValueError):
    """Two launches share an experiment or approval id: one agent per experiment."""


def check_one_agent_per_experiment(launches: Sequence[Launch]) -> None:
    """Approvals are experiment-scoped: no two launches may share an experiment or approval id."""
    for kind in ("experiment_id", "approval_id"):
        seen: dict[UUID, str] = {}
        for launch in launches:
            value = getattr(launch, kind)
            if value in seen:
                raise DuplicateLaunch(
                    f"{kind} {value} is used by more than one launch"
                    f" ({seen[value]} and {launch.policy_ref}); each launch needs its own"
                )
            seen[value] = launch.policy_ref


async def run_demo(
    launches: Sequence[Launch],
    ports: Mapping[Launch, MarketPort],
    policies: Mapping[str, PolicyFactory],
    *,
    bundle: Bundle,
    execution_rule_version: str,
    starting_cash: Decimal,
    runs_dir: Path,
    evaluate: Evaluate | None = None,
) -> list[RunRecord]:
    """Run each launch in turn on its own port, all on one bundle's universe and sessions.

    Every policy reads prices only from its own port."""
    check_one_agent_per_experiment(launches)
    records = []
    for launch in launches:
        port = ports[launch]
        spec = demo_spec(
            launch,
            bundle=bundle,
            execution_rule_version=execution_rule_version,
            starting_cash=starting_cash,
        )
        record, _ = await record_run(
            spec,
            port,
            policies[launch.policy_ref](bundle, port.price_at),
            policy_ref=launch.policy_ref,
            runs_dir=runs_dir,
            evaluate=evaluate,
        )
        records.append(record)
    return records

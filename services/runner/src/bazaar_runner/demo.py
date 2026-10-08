"""The AI Engineer NYC demo: a scripted agent and a baseline over the same simulated fortnight."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from bazaar_protocol import AccountSnapshot, ExperimentContext, OrderRequest, OrderSide

from bazaar_runner.clock import ClockScript, TradingSession
from bazaar_runner.market import MarketPort
from bazaar_runner.policy import Decision, DecisionPolicy, PriceAt
from bazaar_runner.record import Evaluate, RunRecord, record_run
from bazaar_runner.run import DecisionStep, RunSpec

# The demo universe (Anthony, v2): twelve large US stocks, all with bars across the window.
DEMO_SYMBOLS = (
    "AAPL",
    "AMZN",
    "EA",
    "FISV",
    "JNJ",
    "JPM",
    "KO",
    "META",
    "MSFT",
    "NVDA",
    "WMT",
    "XOM",
)
MOMENTUM_REF = "scripted-momentum-v1"
AGENT_FIXTURE_REF = "agent-fixture-v1"
BUY_AND_HOLD_REF = "baseline-buy-and-hold"
CASH_ONLY_REF = "baseline-cash-only"
# Agent and strategy ids are derived from the policy name until the demo goes through the registry.
_IDS = uuid5(NAMESPACE_URL, "https://github.com/duncankmckinnon/bazaar/runner")

# Called once per launch with that launch's own price lookup.
PolicyFactory = Callable[[PriceAt], DecisionPolicy | DecisionStep]


def demo_script() -> ClockScript:
    """2026-02-02..13, ten EST sessions: decide at the 09:30 open, mark at the 16:00 close."""
    days = [date(2026, 2, 2) + timedelta(days=n) for n in range(12)]
    sessions = tuple(
        TradingSession(
            date=day,
            open_at=datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC),
            close_at=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
        )
        for day in days
        if day.weekday() < 5
    )
    return ClockScript(sessions=sessions, decision_offsets=(timedelta(0),))


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


@dataclass(frozen=True)
class Launch:
    policy_ref: str
    experiment_id: UUID
    approval_id: UUID


def demo_spec(
    launch: Launch, *, data_version: str, execution_rule_version: str, starting_cash: Decimal
) -> RunSpec:
    return RunSpec(
        # One run per experiment, so a relaunch reuses the market's idempotent account request.
        run_id=uuid5(launch.experiment_id, "run"),
        experiment_id=launch.experiment_id,
        agent_id=uuid5(_IDS, f"agent:{launch.policy_ref}"),
        strategy_version_id=uuid5(_IDS, f"strategy:{launch.policy_ref}"),
        approval_id=launch.approval_id,
        data_version=data_version,
        execution_rule_version=execution_rule_version,
        starting_cash=starting_cash,
        script=demo_script(),
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
    data_version: str,
    execution_rule_version: str,
    starting_cash: Decimal,
    runs_dir: Path,
    evaluate: Evaluate | None = None,
) -> list[RunRecord]:
    """Run each launch in turn on its own port. Every policy reads prices only from that port."""
    check_one_agent_per_experiment(launches)
    records = []
    for launch in launches:
        port = ports[launch]
        spec = demo_spec(
            launch,
            data_version=data_version,
            execution_rule_version=execution_rule_version,
            starting_cash=starting_cash,
        )
        record, _ = await record_run(
            spec,
            port,
            policies[launch.policy_ref](port.price_at),
            policy_ref=launch.policy_ref,
            runs_dir=runs_dir,
            evaluate=evaluate,
        )
        records.append(record)
    return records

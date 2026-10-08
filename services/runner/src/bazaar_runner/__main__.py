"""`python -m bazaar_runner --demo`: run the demo launches against the market service.

    uv run --package bazaar-runner python -m bazaar_runner --demo --data-version demo-bundle-v1

Four launches: agent-fixture-v1 (the agent places its own order through bazaar_agent.trading),
scripted-momentum-v1 (drop it with --no-momentum), and the buy-and-hold and cash-only baselines.
demo-bundle-v1 (bars, news, filings) is the default because the agent reads news; every launch
uses the one --data-version. alpaca-bars-v1 is bars only; synthetic-v1 is the offline fallback.
Logfire sends only when LOGFIRE_TOKEN is set in the environment; the runner never reads it.

Ids come from flags or environment variables. The runner token comes only from
BAZAAR_RUNNER_TOKEN and is never printed. The agent harness (bazaar_agent.trading), evals
(bazaar_evaluation) and the baselines (bazaar_replay) are on the demo integration branch.
"""

import argparse
import asyncio
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from bazaar_runner.demo import (
    AGENT_FIXTURE_REF,
    BUY_AND_HOLD_REF,
    CASH_ONLY_REF,
    DEMO_SYMBOLS,
    MOMENTUM_REF,
    DuplicateLaunch,
    Launch,
    PolicyFactory,
    ScriptedMomentum,
    check_one_agent_per_experiment,
    run_demo,
)
from bazaar_runner.http_market import DEFAULT_BASE_URL, HttpMarketPort, RunnerConfigError
from bazaar_runner.record import Evaluate
from bazaar_runner.telemetry import configure_telemetry

logger = logging.getLogger("bazaar_runner")
# (flag prefix, policy_ref, deprecated prefixes that still work but warn). Every launch shares
# the schedule, period, starting cash and data version, and has its own experiment, approval
# and port.
DEMO_LAUNCHES = (
    ("agent-fixture", AGENT_FIXTURE_REF, ()),
    ("momentum", MOMENTUM_REF, ("agent",)),
    ("buy-and-hold", BUY_AND_HOLD_REF, ()),
    ("cash-only", CASH_ONLY_REF, ()),
)
KINDS = ("experiment", "approval")


class DemoUnavailable(Exception):
    """A package the demo needs is not installed in this environment."""


def _env_name(prefix: str, kind: str) -> str:
    return f"BAZAAR_{prefix.upper().replace('-', '_')}_{kind.upper()}_ID"


def _env_id(primary: str, *deprecated: str) -> UUID | None:
    if value := os.getenv(primary):
        return UUID(value)
    for name in deprecated:
        if value := os.getenv(name):
            logger.warning("$%s is deprecated: it names the momentum run; set $%s", name, primary)
            return UUID(value)
    return None


class _DeprecatedFlag(argparse.Action):
    """An old flag that still works but warns, so an operator is not misled by its name."""

    def __init__(self, *args, primary: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.primary = primary

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        logger.warning(
            "%s is deprecated: it names the momentum run; use %s", option_string, self.primary
        )
        setattr(namespace, self.dest, values)


def _id_flag(
    parser: argparse.ArgumentParser,
    prefix: str,
    kind: str,
    deprecated: tuple[str, ...] = (),
    *,
    default: UUID | None = None,
) -> None:
    dest = f"{prefix.replace('-', '_')}_{kind}_id"
    primary, env = f"--{prefix}-{kind}-id", _env_name(prefix, kind)
    old_envs = [_env_name(name, kind) for name in deprecated]
    help_text = f"defaults to ${env}"
    if old_envs:
        help_text += " (deprecated: " + ", ".join(f"${e}" for e in old_envs) + ")"
    parser.add_argument(
        primary, dest=dest, type=UUID, default=_env_id(env, *old_envs) or default, help=help_text
    )
    for name in deprecated:
        parser.add_argument(
            f"--{name}-{kind}-id",
            dest=dest,
            type=UUID,
            action=_DeprecatedFlag,
            primary=primary,
            default=argparse.SUPPRESS,
            help=f"deprecated alias for {primary}",
        )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bazaar_runner", description=__doc__)
    parser.add_argument("--demo", action="store_true", required=True, help="run the demo launches")
    parser.add_argument("--no-momentum", action="store_true", help="leave out scripted-momentum-v1")
    parser.add_argument(
        "--no-fiscal-cycles",
        action="store_true",
        help="fallback: give the agent no fiscal cycles, so its filings tool reports unsupported",
    )
    parser.add_argument(
        "--refused-demo",
        action="store_true",
        help="test only: also launch an unlisted approval/experiment pair the market must refuse",
    )
    parser.add_argument("--market-url", default=os.getenv("BAZAAR_MARKET_URL", DEFAULT_BASE_URL))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument(
        "--data-version",
        default="demo-bundle-v1",
        help="one data version for every launch, so they stay comparable: demo-bundle-v1 (bars,"
        " news, filings), alpaca-bars-v1 (bars only) or synthetic-v1",
    )
    parser.add_argument("--execution-rule-version", default="exec-v1")
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal(10000))
    for prefix, _, aliases in DEMO_LAUNCHES:
        for kind in KINDS:
            _id_flag(parser, prefix, kind, aliases)
    # Fresh random ids are never on the market's allow-list.
    for kind in KINDS:
        _id_flag(parser, "refused", kind, default=uuid4())
    args = parser.parse_args(argv)
    missing = [
        f"--{prefix}-{kind}-id"
        for prefix, ref, _ in demo_launches(args)
        for kind in KINDS
        if _ids(args, prefix)[kind] is None
    ]
    if missing:
        parser.error("missing ids: " + ", ".join(missing))
    return args


def demo_launches(args: argparse.Namespace) -> list[tuple[str, str, tuple[str, ...]]]:
    return [row for row in DEMO_LAUNCHES if not (args.no_momentum and row[1] == MOMENTUM_REF)]


def _ids(args: argparse.Namespace, prefix: str) -> dict[str, UUID | None]:
    return {kind: getattr(args, f"{prefix.replace('-', '_')}_{kind}_id") for kind in KINDS}


def load_evaluate() -> Evaluate | None:
    try:
        from bazaar_evaluation import evaluate_and_emit
    except ImportError:
        logger.warning("bazaar_evaluation is not installed; runs will not be evaluated")
        return None
    return evaluate_and_emit


def load_policies(market_url: str, *, fiscal_cycles: bool = True) -> dict[str, PolicyFactory]:
    try:
        import bazaar_agent.trading  # noqa: F401 - fail at startup, not at the first decision
        import pydantic_ai  # noqa: F401
    except ImportError as exc:
        raise DemoUnavailable(f"the agent launch needs bazaar_agent.trading: {exc}") from None
    from bazaar_agent.trading import RuntimeConfig

    from bazaar_runner.agent import (
        AGENT_FIXTURE_INSTRUCTIONS,
        fixture_model_factory,
        make_agent_decider,
    )
    from bazaar_runner.agent_step import AgentStep

    def agent(prices):
        # A fresh decider and fixture model per launch; the agent reads prices itself.
        decider = make_agent_decider(
            AGENT_FIXTURE_INSTRUCTIONS,
            fixture_model_factory(),
            runtime=RuntimeConfig(instrument=True),
            quote_symbols=DEMO_SYMBOLS,
        )
        return AgentStep(
            decider,
            market_url=market_url,
            symbols=DEMO_SYMBOLS,
            read_fiscal_cycles=fiscal_cycles,
        )

    policies: dict[str, PolicyFactory] = {
        AGENT_FIXTURE_REF: agent,
        MOMENTUM_REF: lambda prices: ScriptedMomentum(DEMO_SYMBOLS, prices),
    }
    try:
        from bazaar_replay.baselines import BuyAndHold, CashOnly
    except ImportError:
        logger.warning("bazaar_replay is not installed; skipping the baseline runs")
    else:
        policies[BUY_AND_HOLD_REF] = lambda prices: BuyAndHold(DEMO_SYMBOLS, prices)
        policies[CASH_ONLY_REF] = lambda prices: CashOnly()
    return policies


async def amain(args: argparse.Namespace) -> int:
    policies = load_policies(args.market_url, fiscal_cycles=not args.no_fiscal_cycles)
    from bazaar_agent.strategy_evaluation import strategy_evaluation_session

    launches = []
    for prefix, ref, _ in demo_launches(args):
        if ref in policies:
            ids = _ids(args, prefix)
            launches.append(Launch(ref, ids["experiment"], ids["approval"]))
    if args.refused_demo:
        launches.append(Launch(MOMENTUM_REF, args.refused_experiment_id, args.refused_approval_id))
    # One agent per experiment: refuse before any port, account or order exists.
    check_one_agent_per_experiment(launches)

    async with (
        strategy_evaluation_session(),
        httpx.AsyncClient(base_url=args.market_url, timeout=30) as client,
    ):
        # Built before any run, so a missing token stops the CLI at startup.
        ports = {
            launch: HttpMarketPort.from_env(client, launch.experiment_id, launch.approval_id)
            for launch in launches
        }
        records = await run_demo(
            launches,
            ports,
            policies,
            data_version=args.data_version,
            execution_rule_version=args.execution_rule_version,
            starting_cash=args.starting_cash,
            runs_dir=args.runs_dir,
            evaluate=load_evaluate(),
        )
    for launch, record in zip(launches, records, strict=True):
        outcome = record.status if record.failure_code is None else record.failure_code
        value = record.marks[-1].snapshot.portfolio_value if record.marks else "-"
        print(f"{launch.policy_ref} {launch.experiment_id}: {outcome}, final value {value}")
    print(f"records in {args.runs_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    configure_telemetry()
    try:
        return asyncio.run(amain(args))
    # By name only: any other ValueError (a pydantic ValidationError, say) keeps its traceback.
    except (RunnerConfigError, DemoUnavailable, DuplicateLaunch) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

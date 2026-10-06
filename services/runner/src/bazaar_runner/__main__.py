"""`python -m bazaar_runner --demo`: run the demo launches against the market service.

Ids come from flags or environment variables. The runner token comes only from
BAZAAR_RUNNER_TOKEN and is never printed. Evals (bazaar_evaluation) and the baselines
(bazaar_replay) are optional imports, present on the demo integration branch.
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
    BUY_AND_HOLD_REF,
    CASH_ONLY_REF,
    DEMO_SYMBOLS,
    MOMENTUM_REF,
    Launch,
    PolicyFactory,
    ScriptedMomentum,
    run_demo,
)
from bazaar_runner.http_market import DEFAULT_BASE_URL, HttpMarketPort, RunnerConfigError
from bazaar_runner.record import Evaluate
from bazaar_runner.telemetry import configure_telemetry

logger = logging.getLogger("bazaar_runner")
# Flag prefix -> policy_ref. All three share the schedule, period and starting cash.
APPROVED_RUNS = {
    "agent": MOMENTUM_REF,
    "buy-and-hold": BUY_AND_HOLD_REF,
    "cash-only": CASH_ONLY_REF,
}


def _id_flag(parser: argparse.ArgumentParser, name: str, *, default: UUID | None = None) -> None:
    env = "BAZAAR_" + name.upper().replace("-", "_")
    value = os.getenv(env)
    parser.add_argument(
        f"--{name}",
        type=UUID,
        default=UUID(value) if value else default,
        help=f"defaults to ${env}",
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bazaar_runner", description=__doc__)
    parser.add_argument("--demo", action="store_true", required=True, help="run the demo launches")
    parser.add_argument(
        "--refused-demo",
        action="store_true",
        help="also launch an unlisted approval/experiment pair, which the market must refuse",
    )
    parser.add_argument("--market-url", default=os.getenv("BAZAAR_MARKET_URL", DEFAULT_BASE_URL))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument(
        "--data-version",
        required=True,
        help="the market's imported data version, e.g. synthetic-v1",
    )
    parser.add_argument("--execution-rule-version", default="exec-v1")
    parser.add_argument("--starting-cash", type=Decimal, default=Decimal(10000))
    for name in APPROVED_RUNS:
        _id_flag(parser, f"{name}-experiment-id")
        _id_flag(parser, f"{name}-approval-id")
    # Fresh random ids are never on the market's allow-list.
    _id_flag(parser, "refused-experiment-id", default=uuid4())
    _id_flag(parser, "refused-approval-id", default=uuid4())
    args = parser.parse_args(argv)
    missing = [
        f"--{name}-{kind}-id"
        for name in APPROVED_RUNS
        for kind in ("experiment", "approval")
        if _ids(args, name)[kind] is None
    ]
    if missing:
        parser.error("missing ids: " + ", ".join(missing))
    return args


def _ids(args: argparse.Namespace, name: str) -> dict[str, UUID | None]:
    prefix = name.replace("-", "_")
    return {kind: getattr(args, f"{prefix}_{kind}_id") for kind in ("experiment", "approval")}


def load_evaluate() -> Evaluate | None:
    try:
        from bazaar_evaluation import evaluate_and_emit
    except ImportError:
        logger.warning("bazaar_evaluation is not installed; runs will not be evaluated")
        return None
    return evaluate_and_emit


def load_policies() -> dict[str, PolicyFactory]:
    policies: dict[str, PolicyFactory] = {
        MOMENTUM_REF: lambda prices: ScriptedMomentum(DEMO_SYMBOLS, prices)
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
    policies = load_policies()
    launches = []
    for name, ref in APPROVED_RUNS.items():
        if ref in policies:
            ids = _ids(args, name)
            launches.append(Launch(ref, ids["experiment"], ids["approval"]))
    if args.refused_demo:
        launches.append(Launch(MOMENTUM_REF, args.refused_experiment_id, args.refused_approval_id))

    async with httpx.AsyncClient(base_url=args.market_url, timeout=30) as client:
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
    except RunnerConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

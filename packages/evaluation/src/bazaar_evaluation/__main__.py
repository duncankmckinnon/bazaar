"""Demo fallback: python -m bazaar_evaluation run.json [--out evaluation.json]."""

import argparse
from pathlib import Path

import logfire

from bazaar_evaluation.results import ScoreStatus
from bazaar_evaluation.run_record import evaluate_and_emit


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bazaar_evaluation")
    parser.add_argument("record", type=Path, help="RunRecord v1 JSON file")
    parser.add_argument("--out", type=Path, help="write the RunEvaluation JSON here")
    args = parser.parse_args()

    # Only the CLI configures Logfire; the library leaves it to the host process.
    logfire.configure(send_to_logfire="if-token-present", service_name="bazaar-evals")
    evaluation = evaluate_and_emit(args.record.read_bytes())
    if args.out:
        args.out.write_text(evaluation.model_dump_json(indent=2))

    statuses = [s.status for s in evaluation.trade_scores]
    period = evaluation.period
    print(
        f"orders: {len(statuses)} (scored {statuses.count(ScoreStatus.SCORED)}, "
        f"failed {statuses.count(ScoreStatus.FAILED)}, "
        f"unsupported {statuses.count(ScoreStatus.UNSUPPORTED)})"
    )
    print(f"net_pnl: {period.net_pnl}")
    print(f"return: {period.period_return}")
    print(f"reconciled: {period.reconciled}")


if __name__ == "__main__":
    main()

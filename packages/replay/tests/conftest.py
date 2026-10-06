"""Synthetic RunRecord v1 / RunEvaluation run directories for the leaderboard tests."""

import json
from uuid import UUID, uuid5

import pytest

SAME = object()
NAMESPACE = UUID("00000000-0000-0000-0000-00000000b0a2")
START = "2026-02-02T14:30:00Z"
END = "2026-02-13T21:00:00Z"


def _write_run(
    runs_dir,
    run_id,
    *,
    policy_ref,
    period_return,
    status="completed",
    failure=None,
    orders=("filled",),
    scores=("scored",),
    reconciled=True,
    period_start=START,
    trace_id=None,
    evaluation=True,
    evaluation_account=SAME,
    account=True,
    failure_code=None,
    evaluator_version="evals-v1",
):
    account_id = str(uuid5(NAMESPACE, f"{run_id}-account")) if account else None
    manifest = {
        "experiment_id": str(uuid5(NAMESPACE, "experiment")),
        "agent_id": str(uuid5(NAMESPACE, f"{run_id}-agent")),
        "account_id": account_id,
        "strategy_version_id": str(uuid5(NAMESPACE, f"{run_id}-strategy")),
        "approval_id": str(uuid5(NAMESPACE, "approval")),
        "data_version": "synthetic-v1",
        "execution_rule_version": "immediate-v1",
        "schedule_digest": "sched-v1:9f2c4e1a7b3d5f60",
        "period_start": period_start,
        "period_end": END,
        "starting_cash": "10000",
        "policy_ref": policy_ref,
    }
    if trace_id is not None:
        manifest["trace_id"] = trace_id
    record = {
        "record_version": "runrecord-v1",
        "manifest": manifest,
        "status": status,
        "failure": failure,
        "failure_code": failure_code,
        "orders": [
            {
                "event_sequence": i,
                "order_index": 0,
                "decided_at": START,
                "request": {"symbol": "AAPL"},
                "result": {"status": result, "symbol": "AAPL"},
            }
            for i, result in enumerate(orders)
        ],
        "marks": [],
        "final_account": {"cash": "10000"},
    }
    run_dir = runs_dir / run_id
    run_dir.mkdir()
    (run_dir / "record.json").write_text(json.dumps(record))
    if not evaluation:
        return
    period = {
        "status": "scored" if period_return is not None else "unsupported",
        "period_return": period_return,
        "reconciled": reconciled,
        "evaluator_version": evaluator_version,
    }
    (run_dir / "evaluation.json").write_text(
        json.dumps(
            {
                "experiment_id": manifest["experiment_id"],
                "account_id": account_id if evaluation_account is SAME else evaluation_account,
                "agent_id": manifest["agent_id"],
                "evaluator_version": evaluator_version,
                "run_status": status,
                "run_failure": failure,
                "trade_scores": [{"status": s, "order_id": str(NAMESPACE)} for s in scores],
                "period": period,
            }
        )
    )


def _demo_runs(runs_dir):
    _write_run(
        runs_dir,
        "agent",
        policy_ref="scripted-momentum-v1",
        period_return="0.0520",
        orders=("filled", "filled", "rejected", "filled"),
        scores=("scored", "scored", "pending"),
        trace_id="0af7651916cd43dd8448eb211c80319c",
    )
    _write_run(runs_dir, "bh", policy_ref="baseline-buy-and-hold", period_return="0.0310")
    _write_run(
        runs_dir, "cash", policy_ref="baseline-cash-only", period_return="0", orders=(), scores=()
    )
    _write_run(
        runs_dir,
        "null-return",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        reconciled=False,
    )
    _write_run(
        runs_dir,
        "crashed",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        status="failed",
        failure="decide timed out at event 7",
    )
    _write_run(
        runs_dir,
        "other-period",
        policy_ref="scripted-momentum-v1",
        period_return="0.0900",
        period_start="2026-02-03T14:30:00Z",
    )
    _write_run(
        runs_dir,
        "refused",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        status="failed",
        failure="refused before any account was opened",
        failure_code="approval_denied",
        account=False,
        orders=(),
        evaluation=False,
    )
    _write_run(
        runs_dir, "no-eval", policy_ref="scripted-momentum-v1", period_return="0", evaluation=False
    )


@pytest.fixture
def write_run():
    return _write_run


@pytest.fixture
def demo_runs():
    return _demo_runs

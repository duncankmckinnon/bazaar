import json
from decimal import Decimal
from uuid import UUID, uuid5

from bazaar_replay import MismatchCode
from bazaar_replay.leaderboard import Section, load_board

NAMESPACE = UUID("00000000-0000-0000-0000-00000000b0a2")
START = "2026-02-02T14:30:00Z"
END = "2026-02-13T21:00:00Z"


def write_run(
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
    evaluation_account=None,
):
    account_id = str(uuid5(NAMESPACE, f"{run_id}-account"))
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
        "evaluator_version": "evals-v1",
    }
    (run_dir / "evaluation.json").write_text(
        json.dumps(
            {
                "experiment_id": manifest["experiment_id"],
                "account_id": evaluation_account or account_id,
                "agent_id": manifest["agent_id"],
                "evaluator_version": "evals-v1",
                "run_status": status,
                "run_failure": failure,
                "trade_scores": [{"status": s, "order_id": str(NAMESPACE)} for s in scores],
                "period": period,
            }
        )
    )


def demo_runs(runs_dir):
    write_run(
        runs_dir,
        "agent",
        policy_ref="scripted-momentum-v1",
        period_return="0.0520",
        orders=("filled", "filled", "rejected", "filled"),
        scores=("scored", "scored", "pending"),
        trace_id="0af7651916cd43dd8448eb211c80319c",
    )
    write_run(runs_dir, "bh", policy_ref="baseline-buy-and-hold", period_return="0.0310")
    write_run(
        runs_dir, "cash", policy_ref="baseline-cash-only", period_return="0", orders=(), scores=()
    )
    write_run(
        runs_dir,
        "null-return",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        reconciled=False,
    )
    write_run(
        runs_dir,
        "crashed",
        policy_ref="scripted-momentum-v1",
        period_return=None,
        status="failed",
        failure="decide timed out at event 7",
    )
    write_run(
        runs_dir,
        "other-period",
        policy_ref="scripted-momentum-v1",
        period_return="0.0900",
        period_start="2026-02-03T14:30:00Z",
    )
    write_run(
        runs_dir, "no-eval", policy_ref="scripted-momentum-v1", period_return="0", evaluation=False
    )


def ids(entries):
    return [e.run_id for e in entries]


def test_sections_keep_every_run(tmp_path):
    demo_runs(tmp_path)
    board = load_board(tmp_path)

    assert ids(board.ranked) == ["agent", "bh", "cash", "null-return"]
    assert ids(board.failed) == ["crashed"]
    assert ids(board.not_comparable) == ["other-period"]
    assert ids(board.invalid) == ["no-eval"]
    assert board.reference_run_id == "bh"


def test_excess_is_computed_against_buy_and_hold(tmp_path):
    demo_runs(tmp_path)
    rows = {e.run_id: e for e in load_board(tmp_path).ranked}

    assert rows["agent"].excess_vs_buy_and_hold == Decimal("0.0210")
    assert rows["agent"].excess_computed
    assert rows["cash"].excess_vs_buy_and_hold == Decimal("-0.0310")
    assert rows["bh"].is_reference
    assert rows["bh"].excess_vs_buy_and_hold is None
    assert not rows["bh"].excess_computed


def test_null_return_sorts_last_without_excess_and_flags_unreconciled(tmp_path):
    demo_runs(tmp_path)
    row = load_board(tmp_path).ranked[-1]

    assert row.run_id == "null-return"
    assert row.period_return is None
    assert row.excess_vs_buy_and_hold is None
    assert not row.excess_computed
    assert row.not_reconciled


def test_counts_kind_and_trace(tmp_path):
    demo_runs(tmp_path)
    rows = {e.run_id: e for e in load_board(tmp_path).ranked}
    agent = rows["agent"]

    assert (agent.orders_filled, agent.orders_rejected) == (3, 1)
    assert agent.trade_scores == {"scored": 2, "pending": 1, "failed": 0, "unsupported": 0}
    assert agent.kind == "agent"
    assert agent.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert not agent.not_reconciled
    assert rows["cash"].kind == "baseline"
    assert (rows["cash"].orders_filled, rows["cash"].orders_rejected) == (0, 0)
    assert rows["bh"].trace_id is None  # record written without trace_id still loads


def test_failed_and_not_comparable_rows_say_why(tmp_path):
    demo_runs(tmp_path)
    board = load_board(tmp_path)

    assert board.failed[0].reason == "decide timed out at event 7"
    assert board.failed[0].section is Section.FAILED
    assert board.not_comparable[0].mismatch is MismatchCode.PERIOD_START
    assert board.invalid[0].reason == "evaluation.json is missing"


def test_header_comes_from_the_reference(tmp_path):
    demo_runs(tmp_path)
    header = load_board(tmp_path).header

    assert header.start_at.isoformat() == "2026-02-02T14:30:00+00:00"
    assert header.starting_cash == Decimal(10000)
    assert header.data_version == "synthetic-v1"
    assert header.schedule_digest == "sched-v1:9f2c4e1a"


def test_mismatched_or_unreadable_evaluation_is_invalid(tmp_path):
    write_run(tmp_path, "bh", policy_ref="baseline-buy-and-hold", period_return="0.01")
    write_run(
        tmp_path,
        "swapped",
        policy_ref="scripted-momentum-v1",
        period_return="0.02",
        evaluation_account=str(NAMESPACE),
    )
    write_run(tmp_path, "garbled", policy_ref="scripted-momentum-v1", period_return="0.02")
    (tmp_path / "garbled" / "evaluation.json").write_text("{not json")
    board = load_board(tmp_path)

    reasons = {e.run_id: e.reason for e in board.invalid}
    assert reasons["swapped"] == "record and evaluation have different account_id"
    assert reasons["garbled"].startswith("evaluation.json is unreadable")
    assert ids(board.ranked) == ["bh"]


def test_only_the_runner_baseline_prefix_marks_a_baseline(tmp_path):
    write_run(tmp_path, "bh", policy_ref="baseline-buy-and-hold", period_return="0.01")
    write_run(tmp_path, "colon", policy_ref="baseline:buy-and-hold-v1", period_return="0.03")
    rows = {e.run_id: e for e in load_board(tmp_path).ranked}

    assert rows["bh"].is_reference
    assert rows["colon"].kind == "agent"
    assert rows["colon"].excess_vs_buy_and_hold == Decimal("0.02")


def test_without_buy_and_hold_there_is_no_excess_but_matching_still_applies(tmp_path):
    write_run(tmp_path, "a-agent", policy_ref="scripted-momentum-v1", period_return="0.03")
    write_run(tmp_path, "cash", policy_ref="baseline-cash-only", period_return="0")
    write_run(
        tmp_path,
        "shifted",
        policy_ref="scripted-momentum-v1",
        period_return="0.09",
        period_start="2026-02-03T14:30:00Z",
    )
    board = load_board(tmp_path)

    assert board.reference_run_id is None
    assert ids(board.ranked) == ["a-agent", "cash"]
    assert all(e.excess_vs_buy_and_hold is None for e in board.ranked)
    assert ids(board.not_comparable) == ["shifted"]

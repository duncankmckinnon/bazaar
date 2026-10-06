from decimal import Decimal

from bazaar_replay import MismatchCode
from bazaar_replay.leaderboard import Section, load_board


def ids(entries):
    return [e.run_id for e in entries]


def test_sections_keep_every_run(tmp_path, demo_runs):
    demo_runs(tmp_path)
    board = load_board(tmp_path)

    assert ids(board.ranked) == ["agent", "bh", "cash", "null-return"]
    assert ids(board.failed) == ["crashed"]
    assert ids(board.not_comparable) == ["other-period"]
    assert ids(board.invalid) == ["no-eval"]
    assert board.reference_run_id == "bh"


def test_excess_is_computed_against_buy_and_hold(tmp_path, demo_runs):
    demo_runs(tmp_path)
    rows = {e.run_id: e for e in load_board(tmp_path).ranked}

    assert rows["agent"].excess_vs_buy_and_hold == Decimal("0.0210")
    assert rows["agent"].excess_computed
    assert rows["cash"].excess_vs_buy_and_hold == Decimal("-0.0310")
    assert rows["bh"].is_reference
    assert rows["bh"].excess_vs_buy_and_hold is None
    assert not rows["bh"].excess_computed


def test_null_return_sorts_last_without_excess_and_flags_unreconciled(tmp_path, demo_runs):
    demo_runs(tmp_path)
    row = load_board(tmp_path).ranked[-1]

    assert row.run_id == "null-return"
    assert row.period_return is None
    assert row.excess_vs_buy_and_hold is None
    assert not row.excess_computed
    assert row.not_reconciled


def test_counts_kind_and_trace(tmp_path, demo_runs):
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


def test_failed_and_not_comparable_rows_say_why(tmp_path, demo_runs):
    demo_runs(tmp_path)
    board = load_board(tmp_path)

    assert board.failed[0].reason == "decide timed out at event 7"
    assert board.failed[0].section is Section.FAILED
    assert board.not_comparable[0].mismatch is MismatchCode.PERIOD_START
    assert board.invalid[0].reason == "evaluation.json is missing"


def test_header_comes_from_the_reference(tmp_path, demo_runs):
    demo_runs(tmp_path)
    header = load_board(tmp_path).header

    assert header.start_at.isoformat() == "2026-02-02T14:30:00+00:00"
    assert header.starting_cash == Decimal(10000)
    assert header.data_version == "synthetic-v1"
    assert header.schedule_digest == "sched-v1:9f2c4e1a"


def test_mismatched_or_unreadable_evaluation_is_invalid(tmp_path, write_run):
    write_run(tmp_path, "bh", policy_ref="baseline-buy-and-hold", period_return="0.01")
    write_run(
        tmp_path,
        "swapped",
        policy_ref="scripted-momentum-v1",
        period_return="0.02",
        evaluation_account="00000000-0000-0000-0000-00000000dead",
    )
    write_run(tmp_path, "garbled", policy_ref="scripted-momentum-v1", period_return="0.02")
    (tmp_path / "garbled" / "evaluation.json").write_text("{not json")
    board = load_board(tmp_path)

    reasons = {e.run_id: e.reason for e in board.invalid}
    assert reasons["swapped"] == "record and evaluation have different account_id"
    assert reasons["garbled"].startswith("evaluation.json is unreadable")
    assert ids(board.ranked) == ["bh"]


def test_only_the_runner_baseline_prefix_marks_a_baseline(tmp_path, write_run):
    write_run(tmp_path, "bh", policy_ref="baseline-buy-and-hold", period_return="0.01")
    write_run(tmp_path, "colon", policy_ref="baseline:buy-and-hold-v1", period_return="0.03")
    rows = {e.run_id: e for e in load_board(tmp_path).ranked}

    assert rows["bh"].is_reference
    assert rows["colon"].kind == "agent"
    assert rows["colon"].excess_vs_buy_and_hold == Decimal("0.02")


def test_without_buy_and_hold_there_is_no_excess_but_matching_still_applies(tmp_path, write_run):
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


def test_null_return_sorts_below_a_total_loss(tmp_path, write_run):
    write_run(tmp_path, "a-none", policy_ref="scripted-momentum-v1", period_return=None)
    write_run(tmp_path, "b-total-loss", policy_ref="scripted-momentum-v1", period_return="-1")
    write_run(tmp_path, "c-flat", policy_ref="baseline-cash-only", period_return="0")

    assert ids(load_board(tmp_path).ranked) == ["c-flat", "b-total-loss", "a-none"]

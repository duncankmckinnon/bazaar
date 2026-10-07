import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from bazaar_evaluation import (
    DEMO_CONFIG,
    RunEvaluation,
    ScoreStatus,
    evaluate_and_emit,
)
from bazaar_evaluation.run_record import RunRecord, to_inputs
from pydantic import ValidationError

SAMPLE = Path(__file__).parent / "fixtures" / "runrecord-v1-sample.json"


def sample():
    return json.loads(SAMPLE.read_text())


# The sample under exec-v1 and value-v1 (both 0.01 half-even), 10000 starting cash:
# 07-01 open  buy 7 AAPL @ 150.003 = 1050.021 -> 1050.02; cash 8949.98
#             buy 5 MSFT @ 400     = 2000.00;             cash 6949.98
# 07-01 close AAPL 7 x 152.125 = 1064.875 -> 1064.88; MSFT 5 x 401.10 = 2005.50
#             value 6949.98 + 1064.88 + 2005.50 = 10020.36
# 07-02 open  buy 100 MSFT rejected (insufficient cash); nothing changes
# 07-02 close AAPL 7 x 149 = 1043.00; MSFT 5 x 398 = 1990.00; value 9982.98
# 07-03 open  sell 7 AAPL @ 155.555 = 1088.885 -> 1088.88; cash 8038.86
#             realized 1088.88 - 1050.02 = 38.86
# 07-03 close MSFT 5 x 404.25 = 2021.25; value 8038.86 + 2021.25 = 10060.11
#             unrealized 2021.25 - 2000 = 21.25; net 38.86 + 21.25 = 60.11 = 10060.11 - 10000
#             return 60.11 / 10000 = 0.006011
# Drawdown: peak 10020.36, trough 9982.98, so 37.38 and 37.38 / 10020.36.


def test_sample_evaluates_reconciled_with_hand_calculated_values(capfire):
    evaluation = evaluate_and_emit(sample())
    period = evaluation.period

    assert [s.status for s in evaluation.trade_scores] == [
        ScoreStatus.SCORED,
        ScoreStatus.SCORED,
        ScoreStatus.UNSUPPORTED,
        ScoreStatus.SCORED,
    ]
    assert evaluation.trade_scores[3].realized_pnl == Decimal("38.86")
    assert period.reconciled is True
    assert (period.start_value, period.end_value) == (Decimal(10000), Decimal("10060.11"))
    assert (period.realized_pnl, period.unrealized_pnl) == (Decimal("38.86"), Decimal("21.25"))
    assert period.net_pnl == Decimal("60.11")
    assert period.period_return == Decimal("0.006011")
    assert period.max_drawdown == Decimal("37.38")
    assert period.max_drawdown_fraction == Decimal("37.38") / Decimal("10020.36")


def test_sample_carries_manifest_labels_through(capfire):
    evaluation = evaluate_and_emit(json.dumps(sample()))
    manifest = sample()["manifest"]

    assert evaluation.evaluator_version == "evals-demo-v1"
    assert evaluation.policy_ref == "scripted-momentum-v1"
    assert evaluation.starting_cash == Decimal(10000)
    assert evaluation.period_start.isoformat() == "2025-07-01T13:30:00+00:00"
    assert evaluation.period_end.isoformat() == "2025-07-03T20:00:00+00:00"
    assert evaluation.data_version == manifest["data_version"]
    assert evaluation.execution_rule_version == manifest["execution_rule_version"]
    assert evaluation.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_evaluate_and_emit_produces_the_run_span_and_one_child_per_order(capfire):
    evaluation = evaluate_and_emit(sample())
    exported = capfire.exporter.exported_spans_as_dict()
    (parent,) = [s for s in exported if s["name"] == "evaluate run {experiment_id}"]
    children = [s for s in exported if s["parent"] == parent["context"]]

    assert len(children) == len(evaluation.trade_scores) == 4
    assert parent["attributes"]["reconciled"] is True
    assert parent["attributes"]["net_pnl"] == 60.11
    assert parent["attributes"]["policy_ref"] == "scripted-momentum-v1"


def test_orders_are_adapted_in_event_order():
    record = sample()
    record["orders"].reverse()
    evidence, outcome = to_inputs(RunRecord.model_validate(record))

    assert [o.order_id.int for o in evidence.orders] == [101, 102, 103, 104]
    assert evidence.opening_account.cash == Decimal(10000)
    assert evidence.opening_account.holdings == ()
    assert evidence.context.approval_id == RunRecord.model_validate(record).manifest.approval_id
    assert len(outcome.marks) == 3


def test_unknown_record_version_is_rejected():
    with pytest.raises(ValidationError, match="record_version"):
        evaluate_and_emit(sample() | {"record_version": "runrecord-v2"})


def test_extra_runner_fields_are_ignored(capfire):
    record = sample() | {"runner_build": "abc123"}
    record["manifest"] = record["manifest"] | {"notes": "added later by the runner"}

    assert evaluate_and_emit(record).period.reconciled is True


@pytest.mark.parametrize("trace_id", [None, "absent"])
def test_missing_or_null_trace_id_is_accepted(capfire, trace_id):
    record = sample()
    if trace_id == "absent":
        del record["trace_id"]
    else:
        record["trace_id"] = trace_id

    assert evaluate_and_emit(record).trace_id is None


@pytest.mark.parametrize("trace_id", ["4BF92F3577B34DA6A3CE929D0E0E4736", "abc", "0" * 33])
def test_malformed_trace_id_is_rejected(trace_id):
    with pytest.raises(ValidationError, match="trace_id"):
        evaluate_and_emit(sample() | {"trace_id": trace_id})


def test_failed_run_still_returns_an_evaluation_with_the_failure(capfire):
    record = sample() | {"status": "failed", "failure": "decide() timed out"}
    evaluation = evaluate_and_emit(record)

    assert evaluation.run_status == "failed"
    assert len(evaluation.trade_scores) == 4
    reasons = [e.reason for e in evaluation.period.evidence]
    assert "run failed: decide() timed out" in reasons


def test_cli_runs_without_a_token_and_writes_round_trippable_json(tmp_path):
    out = tmp_path / "evaluation.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGFIRE_")}
    result = subprocess.run(
        [sys.executable, "-m", "bazaar_evaluation", str(SAMPLE), "--out", str(out)],
        cwd=tmp_path,  # keeps any local .logfire credentials directory out of reach
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "reconciled: True" in result.stdout
    assert "net_pnl: 60.11" in result.stdout
    evaluation = RunEvaluation.model_validate_json(out.read_text())
    assert evaluation.period.net_pnl == Decimal("60.11")
    assert json.loads(out.read_text())["period"]["net_pnl"] == "60.11"


def test_demo_config_registers_the_market_rules():
    assert DEMO_CONFIG.evaluator_version == "evals-demo-v1"
    assert DEMO_CONFIG.lot_method == "fifo"
    assert DEMO_CONFIG.horizons == ()
    for rules, version in (
        (DEMO_CONFIG.execution_rules, "exec-v1"),
        (DEMO_CONFIG.valuation_rules, "value-v1"),
    ):
        assert rules[version].quantum == Decimal("0.01")
        assert rules[version].rounding == "half_even"


@pytest.mark.parametrize("failure", [None, "  "])
def test_failed_run_without_a_failure_reason_still_evaluates(capfire, failure):
    # RunRecord v1 allows failure=null even when status is failed.
    evaluation = evaluate_and_emit(sample() | {"status": "failed", "failure": failure})

    assert evaluation.run_status == "failed"
    reasons = [e.reason for e in evaluation.period.evidence]
    assert "run failed: run failed; the record gave no failure reason" in reasons


def test_non_utc_period_end_is_a_malformed_record():
    record = sample()
    record["manifest"] = record["manifest"] | {"period_end": "2025-07-03T16:00:00-04:00"}
    with pytest.raises(ValidationError, match="UTC"):
        evaluate_and_emit(record)


def test_sample_evaluation_round_trips_through_json(capfire):
    evaluation = evaluate_and_emit(sample())

    assert RunEvaluation.model_validate_json(evaluation.model_dump_json()) == evaluation


def refused():
    record = sample() | {
        "status": "failed",
        "failure": "approval refused at cutoff",
        "final_account": None,
        "orders": [],
        "marks": [],
    }
    record["manifest"] = record["manifest"] | {"account_id": None}
    return record


def test_refused_launch_evaluates_as_failed_and_unscored(capfire):
    evaluation = evaluate_and_emit(refused())
    period = evaluation.period

    assert (evaluation.run_status, evaluation.run_failure) == (
        "failed",
        "approval refused at cutoff",
    )
    assert evaluation.account_id is None and period.account_id is None
    assert evaluation.trade_scores == ()
    assert evaluation.policy_ref == "scripted-momentum-v1"
    assert evaluation.approval_id == UUID(sample()["manifest"]["approval_id"])
    assert period.status == ScoreStatus.UNSUPPORTED
    assert period.reconciled is False
    assert "launch refused before an account existed: approval refused at cutoff" in [
        e.reason for e in period.evidence
    ]
    money = ("start_value", "end_value", "market_end_value", "realized_pnl", "unrealized_pnl")
    money += ("fees", "net_pnl", "period_return", "excess_return_vs_cash", "max_drawdown")
    assert all(getattr(period, name) is None for name in money)
    denominators = {d.name: d.value for d in period.denominators}
    assert (denominators["orders"], denominators["marks"]) == (0, 0)
    assert RunEvaluation.model_validate_json(evaluation.model_dump_json()) == evaluation

    exported = capfire.exporter.exported_spans_as_dict()
    (parent,) = [s for s in exported if s["name"] == "evaluate run {experiment_id}"]
    assert [s for s in exported if s["parent"] == parent["context"]] == []
    assert parent["attributes"]["run_status"] == "failed"
    assert parent["attributes"]["period_status"] == "unsupported"
    assert "account_id" not in parent["attributes"]
    assert "launch refused before an account existed" in parent["attributes"]["period_reasons"]


def test_refused_launch_without_a_reason_uses_the_no_reason_wording(capfire):
    evaluation = evaluate_and_emit(refused() | {"failure": None})

    assert evaluation.run_failure == "run failed; the record gave no failure reason"


@pytest.mark.parametrize("missing", ["account_id", "final_account"])
def test_completed_record_without_an_account_is_rejected(missing):
    record = sample()
    if missing == "account_id":
        record["manifest"] = record["manifest"] | {"account_id": None}
    else:
        record["final_account"] = None
    with pytest.raises(ValidationError, match="completed"):
        evaluate_and_emit(record)


@pytest.mark.parametrize("field", ["orders", "marks"])
def test_record_without_an_account_but_with_orders_or_marks_is_rejected(field):
    record = refused() | {field: sample()[field][:1]}
    with pytest.raises(ValidationError, match="account"):
        evaluate_and_emit(record)


def test_failed_run_with_an_account_but_no_final_snapshot_is_unreconciled(capfire):
    record = sample() | {"status": "failed", "failure": "runner crashed", "final_account": None}
    evaluation = evaluate_and_emit(record)

    assert len(evaluation.trade_scores) == 4
    assert evaluation.period.net_pnl == Decimal("60.11")
    assert evaluation.period.reconciled is False
    assert "not reconciled: no final account snapshot" in [
        e.reason for e in evaluation.period.evidence
    ]

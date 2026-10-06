import json
from pathlib import Path

import logfire
import pytest
from bazaar_protocol import OrderRequest, OrderSide
from bazaar_runner.market import ApprovalDenied
from bazaar_runner.record import RunRecord, build_record, record_run
from bazaar_runner.run import run_strategy
from pydantic import BaseModel, ValidationError

from .market_fakes import SESSIONS, SPEC, InMemoryMarket

# Copied from evals: feat/trade-evaluators 157878c,
# packages/evaluation/tests/fixtures/runrecord-v1-sample.json.
SAMPLE = Path(__file__).parent / "fixtures" / "runrecord-v1-sample.json"


async def buy_once(ctx, account):
    if ctx.event_sequence != 0:
        return ()
    return (
        OrderRequest(client_order_id=SPEC.run_id, symbol="AAPL", side=OrderSide.BUY, quantity=5),
    )


class FakeEvaluation(BaseModel):
    policy_ref: str
    orders: int


def fake_evaluate(record: dict) -> FakeEvaluation:
    with logfire.span("evals.fake"):
        return FakeEvaluation(
            policy_ref=record["manifest"]["policy_ref"], orders=len(record["orders"])
        )


def test_evals_sample_round_trips():
    raw = json.loads(SAMPLE.read_text())
    record = RunRecord.model_validate(raw)
    dumped = record.model_dump(mode="json")
    # The runner's own addition; the sample predates it.
    assert dumped.pop("failure_code") is None
    # Pydantic writes UTC as "Z"; the sample spells some times "+00:00". Same instants.
    assert dumped == json.loads(SAMPLE.read_text().replace("+00:00", "Z"))
    assert RunRecord.model_validate(dumped) == record


def test_a_failed_record_must_say_why():
    raw = json.loads(SAMPLE.read_text()) | {"status": "failed", "failure": None}
    with pytest.raises(ValidationError, match="must say why"):
        RunRecord.model_validate(raw)


def test_a_completed_record_must_end_on_the_closing_mark():
    raw = json.loads(SAMPLE.read_text())
    raw["marks"] = raw["marks"][:-1]
    with pytest.raises(ValidationError, match="period_end"):
        RunRecord.model_validate(raw)


async def test_record_from_a_completed_run():
    result = await run_strategy(SPEC, InMemoryMarket(), buy_once)
    record = build_record(SPEC, result, "scripted-test", None)

    assert record.status == "completed" and record.final_account == result.account
    assert record.manifest.account_id == result.account.account_id
    assert record.manifest.period_start == SESSIONS[0].open_at
    assert (
        record.manifest.period_end
        == SESSIONS[-1].close_at
        == record.marks[-1].snapshot.simulated_at
    )
    assert record.marks[-1].event_sequence > record.orders[-1].event_sequence
    (order,) = record.orders
    assert order.decided_at == SESSIONS[0].open_at and order.request.quantity == 5
    assert record.manifest.execution_rule_version == "exec-v1"
    assert {m.snapshot.valuation_rule_version for m in record.marks} == {"value-v1"}
    assert RunRecord.model_validate_json(record.model_dump_json()) == record


async def test_an_approval_denied_run_is_recorded_but_not_evaluated(tmp_path):
    market = InMemoryMarket()

    async def refuse(*args):
        raise ApprovalDenied("This approval does not allow the call")

    market.set_cutoff = refuse
    record, evaluation = await record_run(
        SPEC, market, buy_once, policy_ref="refused", runs_dir=tmp_path, evaluate=fake_evaluate
    )
    assert record.status == "failed" and record.failure_code == "approval_denied"
    assert record.failure.startswith("ApprovalDenied: experiment_not_approved")
    assert record.manifest.account_id is None and record.final_account is None
    assert evaluation is None
    run_dir = tmp_path / str(SPEC.run_id)
    assert RunRecord.model_validate_json((run_dir / "record.json").read_text()) == record
    assert not (run_dir / "evaluation.json").exists()


async def test_record_run_writes_record_and_evaluation(tmp_path):
    record, evaluation = await record_run(
        SPEC,
        InMemoryMarket(),
        buy_once,
        policy_ref="scripted-test",
        runs_dir=tmp_path,
        evaluate=fake_evaluate,
    )
    run_dir = tmp_path / str(SPEC.run_id)
    assert RunRecord.model_validate_json((run_dir / "record.json").read_text()) == record
    assert json.loads((run_dir / "evaluation.json").read_text()) == {
        "policy_ref": "scripted-test",
        "orders": 1,
    }
    assert evaluation == FakeEvaluation(policy_ref="scripted-test", orders=1)


async def test_spans_nest_under_the_run_and_evals_shares_its_trace(capfire, tmp_path):
    record, _ = await record_run(
        SPEC,
        InMemoryMarket(),
        buy_once,
        policy_ref="scripted-test",
        runs_dir=tmp_path,
        evaluate=fake_evaluate,
    )
    spans = capfire.exporter.exported_spans_as_dict()
    by_id = {s["context"]["span_id"]: s for s in spans}

    def named(name):
        return [s for s in spans if s["name"] == name]

    def parent(span):
        return by_id[span["parent"]["span_id"]]

    (run,) = named("runner.run")
    assert run["parent"] is None
    assert run["attributes"]["policy_ref"] == "scripted-test"
    assert run["attributes"]["experiment_id"] == str(SPEC.experiment_id)
    assert run["attributes"]["status"] == "completed"
    decisions, marks = named("runner.decision"), named("runner.mark")
    assert len(decisions) == len(marks) == len(SESSIONS)
    assert all(parent(s) is run for s in decisions + marks)
    assert decisions[0]["attributes"]["event_sequence"] == 0
    (order,) = named("runner.order")
    assert parent(order) is decisions[0]
    assert {k: order["attributes"][k] for k in ("symbol", "side", "quantity", "status")} == {
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "5",
        "status": "filled",
    }
    (evals,) = named("evals.fake")
    assert parent(evals) is run
    trace = run["context"]["trace_id"]
    assert all(s["context"]["trace_id"] == trace for s in spans)
    assert record.trace_id == format(trace, "032x")


async def test_a_failed_run_span_carries_its_failure_code(capfire, tmp_path):
    async def peek(ctx, account):
        raise RuntimeError("policy blew up")

    record, _ = await record_run(SPEC, InMemoryMarket(), peek, policy_ref="p", runs_dir=tmp_path)
    (run,) = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "runner.run"]
    assert run["attributes"]["status"] == "failed"
    assert run["attributes"]["failure_code"] == "policy_error"
    assert record.failure_code == "policy_error"


async def test_real_evals_reconcile_a_fixture_run(tmp_path):
    evaluation = pytest.importorskip("bazaar_evaluation")
    record, result = await record_run(
        SPEC,
        InMemoryMarket(),
        buy_once,
        policy_ref="scripted-test",
        runs_dir=tmp_path,
        evaluate=evaluation.evaluate_and_emit,
    )
    assert record.status == "completed"
    assert result.period.reconciled is True
    assert (tmp_path / str(SPEC.run_id) / "evaluation.json").exists()

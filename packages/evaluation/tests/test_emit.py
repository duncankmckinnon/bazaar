import os
import subprocess
import sys
from uuid import UUID

from bazaar_evaluation import (
    Denominator,
    Evidence,
    PeriodSummary,
    RunEvaluation,
    ScoreStatus,
    TradeScore,
)
from bazaar_evaluation.emit import emit

EXPERIMENT = UUID("00000000-0000-0000-0000-000000000003")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")


def trade(n, side, status, reason="", **metrics):
    return TradeScore(
        order_id=UUID(int=n),
        symbol="AAPL",
        side=side,
        status=status,
        evaluator_version="evals-v1",
        evidence=(
            Evidence(
                order_id=UUID(int=n),
                data_version="synthetic-v1",
                execution_rule_version="exec-v1",
                source="synthetic",
                reason=reason,
            ),
        ),
        **metrics,
    )


def evaluation(reconciled=True):
    gap = () if reconciled else (Evidence(reason="not reconciled: 1 trade score(s) failed"),)
    return RunEvaluation(
        experiment_id=EXPERIMENT,
        account_id=ACCOUNT,
        agent_id=UUID(int=2),
        strategy_version_id=UUID(int=4),
        approval_id=UUID(int=10),
        data_version="synthetic-v1",
        execution_rule_version="exec-v1",
        evaluator_version="evals-v1",
        run_status="completed",
        trade_scores=(
            trade(1, "buy", ScoreStatus.SCORED, realized_pnl="0", closed_quantity="0", fee="0"),
            trade(2, "sell", ScoreStatus.FAILED, "ledger mismatch", fee="0"),
            trade(3, "buy", ScoreStatus.UNSUPPORTED, "order rejected: insufficient_cash"),
        ),
        period=PeriodSummary(
            account_id=ACCOUNT,
            experiment_id=EXPERIMENT,
            status=ScoreStatus.SCORED,
            evaluator_version="evals-v1",
            evidence=(Evidence(data_version="synthetic-v1"), *gap),
            denominators=(Denominator(name="orders", value=3), Denominator(name="fills", value=2)),
            start_value="10000",
            end_value="10200.50",
            realized_pnl="150.25",
            unrealized_pnl="50.25",
            fees="0",
            net_pnl="200.50",
            period_return="0.02005",
            excess_return_vs_cash="0.02005",
            reconciled=reconciled,
        ),
    )


def spans(capfire):
    exported = capfire.exporter.exported_spans_as_dict()
    parents = [s for s in exported if s["name"] == "evaluate run {experiment_id}"]
    assert len(parents) == 1
    children = [s for s in exported if s["parent"] == parents[0]["context"]]
    return parents[0], children


def test_one_parent_span_with_one_child_per_trade_score(capfire):
    emit(evaluation())
    parent, children = spans(capfire)

    assert parent["attributes"]["logfire.msg"] == f"evaluate run {EXPERIMENT}"
    assert parent["attributes"]["logfire.tags"] == ("evaluation",)
    assert [c["attributes"]["logfire.msg"] for c in children] == [
        "trade AAPL buy scored",
        "trade AAPL sell failed",
        "trade AAPL buy unsupported",
    ]
    failed = children[1]["attributes"]
    assert failed["order_id"] == str(UUID(int=2))
    assert failed["reason"] == "ledger mismatch"
    assert failed["data_version"] == "synthetic-v1"
    assert failed["execution_rule_version"] == "exec-v1"
    assert "realized_pnl" not in failed  # None is omitted, not exported as "null"


def test_period_attributes_are_on_the_parent_and_money_is_numeric(capfire):
    emit(evaluation())
    attributes = spans(capfire)[0]["attributes"]

    assert attributes["experiment_id"] == str(EXPERIMENT)
    assert attributes["evaluator_version"] == "evals-v1"
    assert attributes["run_status"] == "completed"
    assert attributes["period_status"] == "scored"
    assert attributes["reconciled"] is True
    assert attributes["net_pnl"] == 200.5
    assert attributes["end_value"] == 10200.5
    assert attributes["period_return"] == 0.02005
    assert attributes["denominator_orders"] == 3
    assert "period_reasons" not in attributes
    assert "max_drawdown" not in attributes


def test_unreconciled_period_reasons_are_on_the_parent(capfire):
    emit(evaluation(reconciled=False))
    attributes = spans(capfire)[0]["attributes"]

    assert attributes["reconciled"] is False
    assert "not reconciled: 1 trade score(s) failed" in attributes["period_reasons"]


def test_emit_without_logfire_configuration_or_token_does_not_raise():
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGFIRE_")}
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from test_emit import evaluation; "
        "from bazaar_evaluation.emit import emit; emit(evaluation()); print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, os.path.dirname(__file__)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

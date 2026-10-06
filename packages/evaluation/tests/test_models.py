from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from bazaar_evaluation import (
    CashAcquisition,
    CashDividend,
    Denominator,
    EvaluationTimeline,
    EvaluatorConfig,
    Evidence,
    InferenceSpend,
    PeriodSummary,
    PriceSeries,
    RunEvidence,
    ScoreStatus,
    Split,
    SymbolChange,
    TradeScore,
    corporate_action_adapter,
)
from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ExecutionErrorDetail,
    FilledOrder,
    Holding,
    PriceObservation,
    RejectedOrder,
)
from pydantic import ValidationError

ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")
AGENT = UUID("00000000-0000-0000-0000-000000000002")
EXPERIMENT = UUID("00000000-0000-0000-0000-000000000003")
STRATEGY = UUID("00000000-0000-0000-0000-000000000004")
ORDER = UUID("00000000-0000-0000-0000-000000000006")
OPEN = datetime(2025, 7, 1, 13, 30, tzinfo=UTC)
FILL_AT = datetime(2025, 7, 1, 14, 30, tzinfo=UTC)
REJECT_AT = datetime(2025, 7, 2, 14, 30, tzinfo=UTC)
END = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)


def account(**updates):
    values = {
        "account_id": ACCOUNT,
        "agent_id": AGENT,
        "experiment_id": EXPERIMENT,
        "strategy_version_id": STRATEGY,
        "simulated_at": OPEN,
        "state_version": 0,
        "cash": "10000",
    }
    return AccountSnapshot(**(values | updates))


def filled():
    # docs/market-agent-api.md example fill, moved into the 2025-26 demo period:
    # 10 AAPL at 74.20, zero fee, so cash 10000 - 742.00 = 9258.00.
    return FilledOrder(
        order_id=ORDER,
        client_order_id=UUID("00000000-0000-0000-0000-000000000005"),
        symbol="AAPL",
        side="buy",
        quantity="10",
        unit_price="74.20",
        fee="0",
        executed_at=FILL_AT,
        price_observed_at=FILL_AT,
        price_available_at=FILL_AT,
        price_source="fixture",
        data_version="fixture-v1",
        execution_rule_version="immediate-v1",
        account=account(
            simulated_at=FILL_AT,
            state_version=1,
            cash="9258.00",
            holdings=(Holding(symbol="AAPL", quantity="10"),),
        ),
    )


def rejected():
    return RejectedOrder(
        order_id=UUID("00000000-0000-0000-0000-000000000008"),
        client_order_id=UUID("00000000-0000-0000-0000-000000000007"),
        symbol="MSFT",
        side="buy",
        quantity="1000",
        rejected_at=REJECT_AT,
        error=ExecutionErrorDetail(code=ErrorCode.INSUFFICIENT_CASH, message="Not enough cash"),
        account=account(
            simulated_at=REJECT_AT,
            state_version=1,
            cash="9258.00",
            holdings=(Holding(symbol="AAPL", quantity="10"),),
        ),
    )


def observation(at, price="100"):
    return PriceObservation(observed_at=at, available_at=at, price=price)


def timeline(observations, **updates):
    values = {
        "start_at": OPEN,
        "end_at": END,
        "source": "fixture",
        "data_version": "fixture-v1",
        "series": (PriceSeries(symbol="AAPL", observations=observations),),
    }
    return EvaluationTimeline(**(values | updates))


def test_run_evidence_accepts_mixed_filled_and_rejected_orders():
    evidence = RunEvidence(opening_account=account(), orders=(filled(), rejected()))

    assert [o.status for o in evidence.orders] == ["filled", "rejected"]
    assert evidence.corporate_actions == ()
    assert evidence.inference_spend == ()
    assert RunEvidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_run_evidence_rejects_orders_out_of_time_order():
    with pytest.raises(ValidationError, match="ordered"):
        RunEvidence(opening_account=account(), orders=(rejected(), filled()))


def test_run_evidence_rejects_an_order_from_another_account():
    other = account(account_id=UUID("00000000-0000-0000-0000-000000000009"))
    with pytest.raises(ValidationError, match="account"):
        RunEvidence(opening_account=other, orders=(filled(),))


def test_inference_spend_rejects_float_usd_and_non_utc_time():
    InferenceSpend(usd="0.0125", input_tokens=1200, output_tokens=300, incurred_at=FILL_AT)
    with pytest.raises(ValidationError, match="decimal strings"):
        InferenceSpend(usd=0.0125, input_tokens=1, output_tokens=1, incurred_at=FILL_AT)
    eastern = FILL_AT.astimezone(timezone(timedelta(hours=-4)))
    with pytest.raises(ValidationError, match="UTC"):
        InferenceSpend(usd="0.01", input_tokens=1, output_tokens=1, incurred_at=eastern)
    with pytest.raises(ValidationError, match="ISO 8601"):
        InferenceSpend(usd="0.01", input_tokens=1, output_tokens=1, incurred_at=1751380200)


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"kind": "split", "symbol": "NVDA", "ratio": "10"}, Split),
        ({"kind": "cash_dividend", "symbol": "AAPL", "amount_per_share": "0.26"}, CashDividend),
        ({"kind": "symbol_change", "old_symbol": "FI", "new_symbol": "FISV"}, SymbolChange),
        ({"kind": "cash_acquisition", "symbol": "K", "cash_per_share": "83.50"}, CashAcquisition),
    ],
)
def test_corporate_action_discriminates_by_kind(payload, kind):
    common = {
        "effective_at": "2025-11-11T14:30:00Z",
        "source": "fixture",
        "data_version": "fixture-v1",
    }
    action = corporate_action_adapter.validate_python(common | payload)

    assert type(action) is kind


def test_corporate_action_rejects_unknown_kind_and_float_amounts():
    common = {"effective_at": "2025-11-11T14:30:00Z", "source": "s", "data_version": "v"}
    with pytest.raises(ValidationError):
        corporate_action_adapter.validate_python(common | {"kind": "delisting", "symbol": "K"})
    with pytest.raises(ValidationError, match="decimal strings"):
        corporate_action_adapter.validate_python(
            common | {"kind": "split", "symbol": "NVDA", "ratio": 10.0}
        )


def test_symbol_change_requires_a_different_symbol():
    with pytest.raises(ValidationError, match="differ"):
        SymbolChange(
            old_symbol="FI",
            new_symbol="FI",
            effective_at=FILL_AT,
            source="fixture",
            data_version="fixture-v1",
        )


def test_timeline_accepts_ordered_observations_inside_its_window():
    tl = timeline((observation(OPEN), observation(FILL_AT), observation(END)))

    assert [o.observed_at for o in tl.series[0].observations] == [OPEN, FILL_AT, END]


def test_timeline_rejects_observation_outside_declared_window():
    with pytest.raises(ValidationError, match="window"):
        timeline((observation(OPEN), observation(END + timedelta(days=1))))
    with pytest.raises(ValidationError, match="window"):
        timeline((observation(OPEN - timedelta(minutes=1)),))


def test_timeline_rejects_out_of_order_or_duplicate_observations():
    with pytest.raises(ValidationError, match="ordered"):
        timeline((observation(FILL_AT), observation(OPEN)))
    with pytest.raises(ValidationError, match="ordered"):
        timeline((observation(FILL_AT), observation(FILL_AT)))


def test_timeline_rejects_inverted_window_and_duplicate_symbols():
    with pytest.raises(ValidationError, match="start_at"):
        timeline((), start_at=END, end_at=OPEN)
    series = PriceSeries(symbol="AAPL", observations=())
    with pytest.raises(ValidationError, match="symbol"):
        timeline((), series=(series, series))


def test_timeline_rejects_float_prices_and_non_utc_window():
    with pytest.raises(ValidationError, match="decimal strings"):
        PriceObservation(observed_at=OPEN, available_at=OPEN, price=101.5)
    with pytest.raises(ValidationError, match="UTC"):
        timeline((), start_at=OPEN.astimezone(timezone(timedelta(hours=-4))))


def test_config_requires_evaluator_version():
    with pytest.raises(ValidationError, match="evaluator_version"):
        EvaluatorConfig()

    config = EvaluatorConfig(evaluator_version="evals-v1")
    assert config.lot_method == "fifo"
    assert config.horizons == ()
    assert config.baseline_symbols == ()


def test_config_rejects_unknown_lot_method():
    EvaluatorConfig(evaluator_version="evals-v1", lot_method="specific_lot")
    with pytest.raises(ValidationError, match="lot_method"):
        EvaluatorConfig(evaluator_version="evals-v1", lot_method="lifo")


@pytest.mark.parametrize("horizon", [timedelta(0), timedelta(days=-1)])
def test_config_rejects_non_positive_horizons(horizon):
    with pytest.raises(ValidationError, match="positive"):
        EvaluatorConfig(evaluator_version="evals-v1", horizons=(timedelta(days=1), horizon))


def test_config_round_trips_through_json():
    config = EvaluatorConfig(
        evaluator_version="evals-v1",
        horizons=(timedelta(days=1), timedelta(days=5)),
        baseline_symbols=("AAPL", "MSFT"),
    )

    assert EvaluatorConfig.model_validate_json(config.model_dump_json()) == config


def test_trade_score_links_order_status_evidence_version_and_denominators():
    evidence = Evidence(
        order_id=ORDER,
        data_version="fixture-v1",
        execution_rule_version="immediate-v1",
        source="fixture",
    )
    score = TradeScore(
        order_id=ORDER,
        status=ScoreStatus.PENDING,
        evaluator_version="evals-v1",
        evidence=(evidence,),
        denominators=(Denominator(name="filled_quantity", value="10"),),
    )

    assert TradeScore.model_validate_json(score.model_dump_json()) == score
    assert {s.value for s in ScoreStatus} == {"pending", "scored", "failed", "unsupported"}


@pytest.mark.parametrize("status", [ScoreStatus.FAILED, ScoreStatus.UNSUPPORTED])
def test_failed_and_unsupported_results_require_a_reason(status):
    without_reason = Evidence(order_id=ORDER, data_version="fixture-v1")
    with pytest.raises(ValidationError, match="reason"):
        TradeScore(
            order_id=ORDER, status=status, evaluator_version="v1", evidence=(without_reason,)
        )
    with pytest.raises(ValidationError, match="reason"):
        PeriodSummary(
            account_id=ACCOUNT, experiment_id=EXPERIMENT, status=status, evaluator_version="v1"
        )

    with_reason = Evidence(order_id=ORDER, reason="no price at horizon")
    TradeScore(order_id=ORDER, status=status, evaluator_version="v1", evidence=(with_reason,))


def test_denominator_rejects_negative_and_float_values():
    with pytest.raises(ValidationError):
        Denominator(name="fills", value="-1")
    with pytest.raises(ValidationError, match="decimal strings"):
        Denominator(name="fills", value=1.0)

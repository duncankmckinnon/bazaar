from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
from bazaar_protocol import (
    AccountSnapshot,
    ApiError,
    ErrorDetail,
    ExecutionErrorDetail,
    ExperimentContext,
    FilledOrder,
    Holding,
    MarkedHolding,
    OrderRequest,
    PortfolioSnapshot,
    PriceHistory,
    PriceHistoryRequest,
    PriceObservation,
    RejectedOrder,
    order_result_adapter,
)
from pydantic import ValidationError

ID = UUID("00000000-0000-0000-0000-000000000001")
TIME = datetime(2020, 1, 2, 14, 30, tzinfo=UTC)


def account(**updates):
    values = {
        "account_id": ID,
        "agent_id": ID,
        "experiment_id": ID,
        "strategy_version_id": ID,
        "simulated_at": TIME,
        "state_version": 1,
        "cash": "9258.00",
        "holdings": (Holding(symbol="AAPL", quantity="10"),),
    }
    return AccountSnapshot(**(values | updates))


def fill(**updates):
    values = {
        "order_id": ID,
        "client_order_id": ID,
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "10",
        "unit_price": "74.20",
        "fee": "0",
        "executed_at": TIME,
        "price_observed_at": TIME,
        "price_available_at": TIME,
        "price_source": "fixture",
        "data_version": "fixture-v1",
        "execution_rule_version": "immediate-v1",
        "account": account(),
    }
    return FilledOrder(**(values | updates))


def test_order_and_account_round_trip():
    request = OrderRequest(client_order_id=ID, symbol="AAPL", side="buy", quantity="10")
    assert OrderRequest.model_validate_json(request.model_dump_json()) == request
    assert AccountSnapshot.model_validate_json(account().model_dump_json()) == account()
    assert account().cash == Decimal("9258.00")
    assert '"cash":"9258.00"' in account().model_dump_json()


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity", "-Infinity", True, 1.5])
def test_order_rejects_invalid_quantities(quantity):
    with pytest.raises(ValidationError):
        OrderRequest(client_order_id=ID, symbol="AAPL", side="buy", quantity=quantity)


@pytest.mark.parametrize("cash", ["-1", "NaN", "Infinity"])
def test_account_rejects_invalid_cash(cash):
    with pytest.raises(ValidationError):
        account(cash=cash)


@pytest.mark.parametrize(
    "updates",
    [
        {"client_order_id": "not-an-id"},
        {"symbol": "aapl"},
        {"side": "hold"},
        {"cash": "1000000"},
        {"approval_id": str(ID)},
        {"simulated_at": TIME},
    ],
)
def test_order_cannot_override_server_context(updates):
    values = {"client_order_id": ID, "symbol": "AAPL", "side": "sell", "quantity": "0.5"}
    with pytest.raises(ValidationError):
        OrderRequest(**(values | updates))


def test_account_rejects_duplicate_holdings_and_boolean_version():
    with pytest.raises(ValidationError):
        account(holdings=(Holding(symbol="AAPL", quantity=1),) * 2)
    with pytest.raises(ValidationError):
        account(state_version=True)


@pytest.mark.parametrize(
    "time",
    [
        TIME.replace(tzinfo=None),
        TIME.astimezone(timezone(timedelta(hours=1))),
        TIME.timestamp(),
        int(TIME.timestamp()),
        str(int(TIME.timestamp())),
    ],
)
def test_timestamps_require_iso_utc(time):
    with pytest.raises(ValidationError):
        account(simulated_at=time)


def test_context_round_trip():
    context = ExperimentContext(
        experiment_id=ID,
        agent_id=ID,
        account_id=ID,
        strategy_version_id=ID,
        approval_id=ID,
        simulated_at=TIME,
        event_sequence=0,
        data_version="fixture-v1",
        execution_rule_version="immediate-v1",
    )
    assert ExperimentContext.model_validate_json(context.model_dump_json()) == context


def test_discriminated_results_and_schema():
    rejected = RejectedOrder(
        order_id=ID,
        client_order_id=ID,
        symbol="AAPL",
        side="sell",
        quantity="100",
        rejected_at=TIME,
        error=ExecutionErrorDetail(code="insufficient_holdings", message="Only 10 shares owned"),
        account=account(),
    )
    for result in (fill(), rejected):
        assert order_result_adapter.validate_json(result.model_dump_json()) == result
    assert order_result_adapter.json_schema()["discriminator"]["propertyName"] == "status"
    with pytest.raises(ValidationError):
        order_result_adapter.validate_python({"status": "pending"})


@pytest.mark.parametrize(
    "code", ["unauthorized", "forbidden", "invalid_request", "idempotency_conflict"]
)
def test_transport_errors_cannot_be_execution_rejections(code):
    with pytest.raises(ValidationError):
        ExecutionErrorDetail(code=code, message="Not an execution result")


def test_fill_validates_price_availability_snapshot_and_fee():
    for updates in (
        {"price_observed_at": TIME + timedelta(seconds=1)},
        {"price_available_at": TIME + timedelta(seconds=1)},
        {"price_available_at": TIME - timedelta(seconds=1)},
        {"account": account(simulated_at=TIME - timedelta(seconds=1))},
        {"fee": "-1"},
    ):
        with pytest.raises(ValidationError):
            fill(**updates)


def test_rejection_rejects_wrong_snapshot_time():
    with pytest.raises(ValidationError):
        RejectedOrder(
            order_id=ID,
            client_order_id=ID,
            symbol="AAPL",
            side="buy",
            quantity=1,
            rejected_at=TIME,
            error=ExecutionErrorDetail(code="insufficient_cash", message="Not enough cash"),
            account=account(simulated_at=TIME - timedelta(seconds=1)),
        )


def history(points, **updates):
    values = {
        "experiment_id": ID,
        "symbol": "AAPL",
        "cutoff_at": TIME,
        "source": "fixture",
        "data_version": "fixture-v1",
        "observations": points,
    }
    return PriceHistory(**(values | updates))


def test_history_round_trip_and_delayed_availability():
    point = PriceObservation(observed_at=TIME - timedelta(days=1), available_at=TIME, price="74.20")
    result = history((point,))
    assert PriceHistory.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        history((point,), cutoff_at=TIME - timedelta(seconds=1))


def test_history_rejects_early_availability_and_unordered_duplicates():
    with pytest.raises(ValidationError):
        PriceObservation(observed_at=TIME, available_at=TIME - timedelta(seconds=1), price=1)
    point = PriceObservation(observed_at=TIME, available_at=TIME, price=1)
    earlier = PriceObservation(observed_at=TIME - timedelta(days=1), available_at=TIME, price=1)
    for points in ((point, point), (point, earlier)):
        with pytest.raises(ValidationError):
            history(points)


@pytest.mark.parametrize(
    "updates",
    [
        {"end_at": TIME - timedelta(seconds=1)},
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"limit": "1.5"},
        {"cursor": ""},
    ],
)
def test_history_request_rejects_invalid_windows_limits_and_cursor(updates):
    with pytest.raises(ValidationError):
        PriceHistoryRequest(**({"symbol": "AAPL", "start_at": TIME, "end_at": TIME} | updates))


def test_history_request_accepts_http_query_limit():
    query = PriceHistoryRequest(symbol="AAPL", start_at=TIME, end_at=TIME, limit="100")
    assert query.limit == 100


def portfolio(**updates):
    holding = MarkedHolding(
        symbol="AAPL",
        quantity="10",
        unit_mark="74.20",
        mark_observed_at=TIME,
        mark_available_at=TIME,
    )
    values = {
        "account_id": ID,
        "experiment_id": ID,
        "simulated_at": TIME,
        "state_version": 1,
        "cash": "9258",
        "holdings": (holding,),
        "portfolio_value": "10000",
        "valuation_rule_version": "fixture-v1",
        "source": "fixture",
        "data_version": "fixture-v1",
    }
    return PortfolioSnapshot(**(values | updates))


def test_portfolio_round_trip_and_cutoff():
    result = portfolio()
    assert PortfolioSnapshot.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        portfolio(simulated_at=TIME - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        portfolio(holdings=result.holdings * 2)


def test_portfolio_validation_does_not_depend_on_decimal_context():
    with localcontext() as context:
        context.prec = 2
        assert portfolio().portfolio_value == Decimal(10000)
    # Numeric valuation/rounding follows the server's versioned policy, not ambient wire arithmetic.
    zero_mark = MarkedHolding(
        symbol="AAPL", quantity=10, unit_mark=0, mark_observed_at=TIME, mark_available_at=TIME
    )
    assert portfolio(holdings=(zero_mark,), portfolio_value="9258").holdings[0].unit_mark == 0


def test_error_round_trip_and_frozen_snapshot():
    error = ApiError(error=ErrorDetail(code="idempotency_conflict", message="Different order body"))
    assert ApiError.model_validate_json(error.model_dump_json()) == error
    with pytest.raises(ValidationError):
        account().cash = Decimal(0)

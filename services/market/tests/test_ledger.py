import sqlite3
import threading
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from bazaar_market import db
from bazaar_market.ledger import Ledger
from bazaar_protocol import ErrorCode, FilledOrder, OrderRequest, RejectedOrder

from .ledger_fakes import BARS, DAY1_CLOSE, DAY2_CLOSE, FakePrices, close


def make_ledger(path, prices=None) -> Ledger:
    ledger = Ledger(path, prices or FakePrices(BARS))
    ledger.initialize()
    return ledger


def open_account(ledger: Ledger, cash: str = "1000.00") -> tuple[UUID, UUID]:
    eid = uuid4()
    ledger.set_cutoff(eid, DAY1_CLOSE, "fixture-v1", "exec-v1")
    account = ledger.create_account(
        eid, request_id=uuid4(), agent_id=uuid4(), strategy_version_id=uuid4(), cash=Decimal(cash)
    )
    return eid, account.account_id


def order(side: str, quantity: str, symbol: str = "AAPL", client_order_id=None) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id or uuid4(), symbol=symbol, side=side, quantity=quantity
    )


@pytest.fixture
def ledger(tmp_path):
    return make_ledger(tmp_path / "market.db")


def test_buy_fills_at_the_close_available_by_the_cutoff(ledger):
    eid, aid = open_account(ledger)
    result = ledger.submit(eid, aid, order("buy", "3"))
    assert isinstance(result, FilledOrder)
    assert result.unit_price == Decimal("100.00")
    assert result.executed_at == result.price_available_at == DAY1_CLOSE
    assert (result.price_source, result.data_version, result.execution_rule_version) == (
        "fixture",
        "fixture-v1",
        "exec-v1",
    )
    assert result.account.cash == Decimal("700.00")
    assert result.account.state_version == 1
    assert ledger.account(eid, aid) == result.account


def test_exec_v1_rounds_a_half_cent_tie_once_half_even(ledger):
    eid, aid = open_account(ledger)
    result = ledger.submit(eid, aid, order("buy", "1", symbol="TIE"))
    assert result.unit_price == Decimal("100.005")
    assert result.account.cash == Decimal("900.00")


def test_overspend_is_rejected_without_changing_state(ledger):
    eid, aid = open_account(ledger)
    result = ledger.submit(eid, aid, order("buy", "11"))
    assert isinstance(result, RejectedOrder)
    assert result.error.code == ErrorCode.INSUFFICIENT_CASH
    assert result.account.state_version == 0
    assert ledger.account(eid, aid).cash == Decimal("1000.00")


def test_oversell_is_rejected_without_changing_state(ledger):
    eid, aid = open_account(ledger)
    ledger.submit(eid, aid, order("buy", "2"))
    result = ledger.submit(eid, aid, order("sell", "3"))
    assert isinstance(result, RejectedOrder)
    assert result.error.code == ErrorCode.INSUFFICIENT_HOLDINGS
    assert result.account.state_version == 1
    assert ledger.account(eid, aid).holdings[0].quantity == Decimal(2)


def test_sell_after_the_clock_advances_uses_the_new_close(ledger):
    eid, aid = open_account(ledger)
    ledger.submit(eid, aid, order("buy", "2"))
    ledger.set_cutoff(eid, DAY2_CLOSE)
    result = ledger.submit(eid, aid, order("sell", "2"))
    assert result.unit_price == Decimal("110.00")
    assert result.account.cash == Decimal("1020.00")
    assert result.account.holdings == ()


def test_missing_price_is_a_rejection(ledger):
    eid, aid = open_account(ledger)
    result = ledger.submit(eid, aid, order("buy", "1", symbol="MSFT"))
    assert result.error.code == ErrorCode.DATA_UNAVAILABLE


def test_a_retry_returns_the_original_result_without_a_second_debit(ledger):
    eid, aid = open_account(ledger)
    request = order("buy", "1")
    first = ledger.submit(eid, aid, request)
    ledger.set_cutoff(eid, DAY2_CLOSE)
    assert ledger.submit(eid, aid, request) == first
    assert ledger.account(eid, aid).cash == Decimal("900.00")


def test_a_retry_with_a_different_body_conflicts(ledger):
    eid, aid = open_account(ledger)
    request = order("buy", "1")
    ledger.submit(eid, aid, request)
    with pytest.raises(db.MarketError) as error:
        ledger.submit(eid, aid, order("buy", "2", client_order_id=request.client_order_id))
    assert (error.value.status_code, error.value.code) == (409, ErrorCode.IDEMPOTENCY_CONFLICT)


def test_fractional_quantity_is_refused_and_nothing_is_stored(ledger):
    eid, aid = open_account(ledger)
    with pytest.raises(db.MarketError) as error:
        ledger.submit(eid, aid, order("buy", "1.5"))
    assert error.value.status_code == 422
    assert ledger.submit(eid, aid, order("buy", "1")).account.state_version == 1


def test_concurrent_buys_cannot_both_spend_the_same_cash(tmp_path):
    # Both writers reach the price lookup before either writes, unless the first one holds the
    # write lock from BEGIN. A deferred BEGIN lets both read 1000.00; one then fails with
    # "database is locked" and this test fails.
    barrier = threading.Barrier(2)

    def both_in_flight():
        try:
            barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass

    ledger = make_ledger(tmp_path / "market.db", FakePrices(BARS, on_price=both_in_flight))
    eid, aid = open_account(ledger)
    results, errors = [], []

    def buy():
        try:
            results.append(ledger.submit(eid, aid, order("buy", "6")))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=buy) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(r.status for r in results) == ["filled", "rejected"]
    assert ledger.account(eid, aid).cash == Decimal("400.00")


def test_state_survives_a_restart(tmp_path):
    eid, aid = open_account(make_ledger(tmp_path / "market.db"))
    make_ledger(tmp_path / "market.db").submit(eid, aid, order("buy", "1"))
    reopened = make_ledger(tmp_path / "market.db")
    assert reopened.account(eid, aid).cash == Decimal("900.00")
    assert reopened.account(eid, aid).state_version == 1


def test_fills_are_immutable(tmp_path):
    ledger = make_ledger(tmp_path / "market.db")
    eid, aid = open_account(ledger)
    ledger.submit(eid, aid, order("buy", "1"))
    with db.read_connection(tmp_path / "market.db") as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE acct_fills SET unit_price = '1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM acct_orders")


def test_unknown_rule_or_data_version_is_refused(ledger):
    with pytest.raises(db.MarketError) as error:
        ledger.set_cutoff(uuid4(), DAY1_CLOSE, "fixture-v1", "exec-v9")
    assert error.value.status_code == 422
    with pytest.raises(db.MarketError) as error:
        ledger.set_cutoff(uuid4(), DAY1_CLOSE, "other-v1", "exec-v1")
    assert error.value.status_code == 422


def test_account_before_any_cutoff_is_refused(ledger):
    with pytest.raises(db.MarketError) as error:
        ledger.create_account(
            uuid4(),
            request_id=uuid4(),
            agent_id=uuid4(),
            strategy_version_id=uuid4(),
            cash=Decimal(1),
        )
    assert (error.value.status_code, error.value.code) == (409, ErrorCode.EXPERIMENT_NOT_RUNNING)


def test_create_account_is_idempotent_on_request_id(ledger):
    eid = uuid4()
    ledger.set_cutoff(eid, DAY1_CLOSE, "fixture-v1", "exec-v1")
    body = {"request_id": uuid4(), "agent_id": uuid4(), "strategy_version_id": uuid4()}
    first = ledger.create_account(eid, **body, cash=Decimal(100))
    assert ledger.create_account(eid, **body, cash=Decimal("100.00")) == first
    with pytest.raises(db.MarketError) as error:
        ledger.create_account(eid, **body, cash=Decimal(200))
    assert error.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(db.MarketError):
        ledger.create_account(eid, **{**body, "request_id": uuid4()}, cash=Decimal(100))


def test_closed_account_refuses_orders_and_close_is_repeatable(ledger):
    eid, aid = open_account(ledger)
    ledger.submit(eid, aid, order("buy", "1"))
    closed = ledger.close_account(eid, aid)
    ledger.set_cutoff(eid, DAY2_CLOSE)
    assert ledger.close_account(eid, aid) == closed
    assert ledger.account(eid, aid).simulated_at == DAY1_CLOSE
    with pytest.raises(db.MarketError) as error:
        ledger.submit(eid, aid, order("buy", "1"))
    assert (error.value.status_code, error.value.code) == (409, ErrorCode.EXPERIMENT_NOT_RUNNING)


def test_portfolio_marks_at_the_cutoff(ledger):
    eid, aid = open_account(ledger)
    ledger.submit(eid, aid, order("buy", "2"))
    assert ledger.portfolio(eid, aid).portfolio_value == Decimal("1000.00")
    ledger.set_cutoff(eid, DAY2_CLOSE)
    portfolio = ledger.portfolio(eid, aid)
    assert portfolio.holdings[0].unit_mark == Decimal("110.00")
    assert portfolio.portfolio_value == Decimal("1020.00")
    assert portfolio.valuation_rule_version == "value-v1"


def test_value_v1_rounds_each_holding_once_half_even(tmp_path):
    prices = FakePrices({"A": [close(DAY1_CLOSE, "10.00")], "B": [close(DAY1_CLOSE, "10.00")]})
    ledger = make_ledger(tmp_path / "market.db", prices)
    eid, aid = open_account(ledger, cash="100.00")
    ledger.submit(eid, aid, order("buy", "1", symbol="A"))
    ledger.submit(eid, aid, order("buy", "1", symbol="B"))
    prices.bars["A"].append(close(DAY2_CLOSE, "10.005"))
    prices.bars["B"].append(close(DAY2_CLOSE, "10.015"))
    ledger.set_cutoff(eid, DAY2_CLOSE)
    # A: 10.005 -> 10.00, B: 10.015 -> 10.02 (half-even), cash 80.00, no further rounding.
    assert ledger.portfolio(eid, aid).portfolio_value == Decimal("100.02")

"""Accounts, orders and fills: the market's authority for cash and holdings.

Execution follows the v0.1 contract: an order fills in full at once or is rejected. Each order is
one BEGIN IMMEDIATE transaction covering the idempotency lookup, the price, the balance check and
the write, so concurrent orders cannot spend the same cash or sell the same shares.
"""

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import logfire
from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ExecutionErrorDetail,
    FilledOrder,
    Holding,
    MarkedHolding,
    OrderRequest,
    OrderSide,
    PortfolioSnapshot,
    PriceObservation,
    RejectedOrder,
    order_result_adapter,
)

from bazaar_market import db
from bazaar_market.clock import Experiment, SqliteClock, UnknownExperiment, load_experiment
from bazaar_market.history import PageScope
from bazaar_market.prices import MissingData, TradingSession

CENT = Decimal("0.01")
# How far back the fill rule looks for the latest session. No US market closure has come close.
SESSION_LOOKBACK = timedelta(days=14)

SCHEMA = """
CREATE TABLE IF NOT EXISTS acct_accounts (
    account_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES acct_experiments(experiment_id),
    agent_id TEXT NOT NULL,
    strategy_version_id TEXT NOT NULL,
    cash_cents INTEGER NOT NULL CHECK (cash_cents >= 0),
    state_version INTEGER NOT NULL CHECK (state_version >= 0),
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    closed_at TEXT,
    request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_response TEXT NOT NULL,
    UNIQUE (experiment_id, request_id),
    UNIQUE (experiment_id, agent_id),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS acct_holdings (
    account_id TEXT NOT NULL REFERENCES acct_accounts(account_id),
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (account_id, symbol)
);
CREATE TABLE IF NOT EXISTS acct_orders (
    order_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES acct_accounts(account_id),
    client_order_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('filled', 'rejected')),
    result TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE (account_id, client_order_id)
);
CREATE TABLE IF NOT EXISTS acct_fills (
    order_id TEXT PRIMARY KEY REFERENCES acct_orders(order_id),
    account_id TEXT NOT NULL REFERENCES acct_accounts(account_id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price TEXT NOT NULL,
    notional_cents INTEGER NOT NULL CHECK (notional_cents > 0),
    cash_after_cents INTEGER NOT NULL CHECK (cash_after_cents >= 0),
    state_version_after INTEGER NOT NULL,
    executed_at TEXT NOT NULL,
    price_observed_at TEXT NOT NULL,
    price_available_at TEXT NOT NULL,
    price_source TEXT NOT NULL,
    data_version TEXT NOT NULL,
    execution_rule_version TEXT NOT NULL,
    CHECK (price_observed_at <= price_available_at AND price_available_at <= executed_at)
);
CREATE TRIGGER IF NOT EXISTS acct_orders_immutable_update BEFORE UPDATE ON acct_orders
BEGIN SELECT RAISE(ABORT, 'orders are immutable'); END;
CREATE TRIGGER IF NOT EXISTS acct_orders_immutable_delete BEFORE DELETE ON acct_orders
BEGIN SELECT RAISE(ABORT, 'orders are immutable'); END;
CREATE TRIGGER IF NOT EXISTS acct_fills_immutable_update BEFORE UPDATE ON acct_fills
BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS acct_fills_immutable_delete BEFORE DELETE ON acct_fills
BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
"""


class PriceSource(Protocol):
    """One data version's prices. `price_at` raises MissingData when nothing is available.

    exec-v1 and value-v1 both use the close of the latest daily bar available by the cutoff.
    """

    data_version: str
    price_source: str

    def price_at(self, symbol: str, cutoff: datetime) -> PriceObservation: ...

    def session(self, day: date) -> TradingSession | None:
        """The session on `day`, or None when the data version has no bars that day."""
        ...


@dataclass(frozen=True)
class ExecutionRule:
    """exec-v1: no fee, whole shares, notional quantized once per fill to the cent, half-even."""

    version: str
    valuation_rule_version: str

    def notional(self, quantity: int, unit_price: Decimal) -> Decimal:
        return (quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_EVEN)

    def market_value(self, quantity: int, mark: Decimal) -> Decimal:
        """value-v1: each holding is quantized once; the portfolio sum is not re-rounded."""
        return (quantity * mark).quantize(CENT, rounding=ROUND_HALF_EVEN)


RULES = {"exec-v1": ExecutionRule(version="exec-v1", valuation_rule_version="value-v1")}


def latest_session(prices: PriceSource, cutoff: datetime) -> TradingSession | None:
    """The most recent session whose close is at or before `cutoff`, from the data version's own
    calendar. None if there is none within SESSION_LOOKBACK.
    """
    day = cutoff.date()
    while day >= (cutoff - SESSION_LOOKBACK).date():
        session = prices.session(day)
        if session is not None and session.close_at <= cutoff:
            return session
        day -= timedelta(days=1)
    return None


def rule_for(version: str) -> ExecutionRule:
    try:
        return RULES[version]
    except KeyError:
        raise db.MarketError(
            422, ErrorCode.INVALID_REQUEST, f"Unknown execution rule {version}"
        ) from None


def to_cents(amount: Decimal) -> int:
    if amount != amount.quantize(CENT):
        raise db.MarketError(422, ErrorCode.INVALID_REQUEST, "Cash must be whole cents")
    return int(amount * 100)


def from_cents(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def whole_shares(quantity: Decimal) -> int:
    if quantity != quantity.to_integral_value():
        raise db.MarketError(422, ErrorCode.INVALID_REQUEST, "exec-v1 trades whole shares only")
    return int(quantity)


def fingerprint(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class _Account:
    account_id: UUID
    experiment_id: UUID
    agent_id: UUID
    strategy_version_id: UUID
    cash_cents: int
    state_version: int
    closed_at: datetime | None
    holdings: dict[str, int]


class Ledger:
    def __init__(self, database_path: Path, prices_for: Callable[[str], PriceSource]) -> None:
        self.database_path = database_path
        self.prices_for = prices_for
        self.clock = SqliteClock(database_path)

    def initialize(self) -> None:
        db.initialize(self.database_path, SCHEMA)

    def set_cutoff(
        self,
        experiment_id: UUID,
        cutoff: datetime,
        data_version: str | None = None,
        execution_rule_version: str | None = None,
    ) -> Experiment:
        if execution_rule_version is not None:
            rule_for(execution_rule_version)
        if data_version is not None:
            try:
                _ = self.prices_for(data_version).price_source  # raises if not imported
            except MissingData:
                raise db.MarketError(
                    422, ErrorCode.INVALID_REQUEST, f"Data version {data_version} is not loaded"
                ) from None
        return self.clock.set_cutoff(experiment_id, cutoff, data_version, execution_rule_version)

    def create_account(
        self,
        experiment_id: UUID,
        *,
        request_id: UUID,
        agent_id: UUID,
        strategy_version_id: UUID,
        cash: Decimal,
    ) -> AccountSnapshot:
        cents = to_cents(cash)
        body = fingerprint(
            {"agent_id": agent_id, "strategy_version_id": strategy_version_id, "cash": cents}
        )
        with db.write_transaction(self.database_path, operation="create_account") as connection:
            experiment = self._running_experiment(connection, experiment_id)
            replay = connection.execute(
                "SELECT request_fingerprint, created_response FROM acct_accounts "
                "WHERE experiment_id = ? AND request_id = ?",
                (str(experiment_id), str(request_id)),
            ).fetchone()
            if replay is not None:
                if replay["request_fingerprint"] != body:
                    raise db.MarketError(
                        409, ErrorCode.IDEMPOTENCY_CONFLICT, "Different account request body"
                    )
                return AccountSnapshot.model_validate_json(replay["created_response"])
            existing = connection.execute(
                "SELECT 1 FROM acct_accounts WHERE experiment_id = ? AND agent_id = ?",
                (str(experiment_id), str(agent_id)),
            ).fetchone()
            if existing is not None:
                raise db.MarketError(
                    409,
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "This agent already has an account in this experiment",
                )
            account_id = uuid4()
            snapshot = AccountSnapshot(
                account_id=account_id,
                agent_id=agent_id,
                experiment_id=experiment_id,
                strategy_version_id=strategy_version_id,
                simulated_at=experiment.cutoff_at,
                state_version=0,
                cash=from_cents(cents),
            )
            connection.execute(
                "INSERT INTO acct_accounts VALUES (?, ?, ?, ?, ?, 0, 'open', NULL, ?, ?, ?)",
                (
                    str(account_id),
                    str(experiment_id),
                    str(agent_id),
                    str(strategy_version_id),
                    cents,
                    str(request_id),
                    body,
                    snapshot.model_dump_json(),
                ),
            )
            return snapshot

    def account(self, experiment_id: UUID, account_id: UUID) -> AccountSnapshot:
        with db.read_connection(self.database_path, operation="account") as connection:
            experiment = self._experiment(connection, experiment_id)
            account = self._load(connection, experiment_id, account_id)
            return self._snapshot(account, account.closed_at or experiment.cutoff_at)

    def submit(
        self, experiment_id: UUID, account_id: UUID, order: OrderRequest
    ) -> FilledOrder | RejectedOrder:
        with logfire.span(
            "order {side} {quantity} {symbol}",
            experiment_id=str(experiment_id),
            account_id=str(account_id),
            client_order_id=str(order.client_order_id),
            symbol=order.symbol,
            side=order.side.value,
            quantity=str(order.quantity),
        ) as span:
            result = self._submit(experiment_id, account_id, order)
            span.set_attribute("result", result.status)
            if isinstance(result, FilledOrder):
                span.set_attribute("unit_price", str(result.unit_price))
                span.set_attribute("executed_at", result.executed_at.isoformat())
                span.set_attribute("cash_after", str(result.account.cash))
            else:
                span.set_attribute("error_code", result.error.code.value)
            return result

    def _submit(
        self, experiment_id: UUID, account_id: UUID, order: OrderRequest
    ) -> FilledOrder | RejectedOrder:
        body = fingerprint(
            {
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": str(order.quantity.normalize()),
            }
        )
        with db.write_transaction(self.database_path, operation="submit") as connection:
            experiment = self._experiment(connection, experiment_id)
            account = self._load(connection, experiment_id, account_id)
            replay = connection.execute(
                "SELECT fingerprint, result FROM acct_orders "
                "WHERE account_id = ? AND client_order_id = ?",
                (str(account_id), str(order.client_order_id)),
            ).fetchone()
            if replay is not None:
                if replay["fingerprint"] != body:
                    raise db.MarketError(
                        409, ErrorCode.IDEMPOTENCY_CONFLICT, "Different order body"
                    )
                return order_result_adapter.validate_json(replay["result"])
            if account.closed_at is not None:
                raise db.MarketError(409, ErrorCode.EXPERIMENT_NOT_RUNNING, "The account is closed")
            rule = rule_for(experiment.execution_rule_version)
            quantity = whole_shares(order.quantity)
            now = experiment.cutoff_at
            order_id = self._next_order_id(connection)
            result = self._execute(
                connection, experiment, rule, account, order, quantity, now, order_id
            )
            connection.execute(
                "INSERT INTO acct_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(result.order_id),
                    str(account_id),
                    str(order.client_order_id),
                    body,
                    result.status,
                    result.model_dump_json(),
                    db.format_time(now),
                ),
            )
            if isinstance(result, FilledOrder):
                self._record_fill(connection, result, rule)
            return result

    def close_account(self, experiment_id: UUID, account_id: UUID) -> AccountSnapshot:
        """Freeze the account at the current cutoff. Holdings are kept, not liquidated."""
        with db.write_transaction(self.database_path, operation="close_account") as connection:
            experiment = self._experiment(connection, experiment_id)
            account = self._load(connection, experiment_id, account_id)
            if account.closed_at is not None:
                return self._snapshot(account, account.closed_at)
            connection.execute(
                "UPDATE acct_accounts SET status = 'closed', closed_at = ? WHERE account_id = ?",
                (db.format_time(experiment.cutoff_at), str(account_id)),
            )
            return self._snapshot(account, experiment.cutoff_at)

    def page_scope(self, experiment_id: UUID, account_id: UUID) -> PageScope:
        """The research scope for an account named by the caller. A foreign account is 403."""
        with db.read_connection(self.database_path, operation="page_scope") as connection:
            experiment = self._experiment(connection, experiment_id)
            try:
                account = self._load(connection, experiment_id, account_id)
            except db.MarketError:
                raise db.MarketError(
                    403, ErrorCode.FORBIDDEN, "The account is not in this experiment"
                ) from None
        return self._page_scope(experiment, account)

    def order_history(
        self, experiment_id: UUID, account_id: UUID
    ) -> tuple[PageScope, list[FilledOrder | RejectedOrder]]:
        """Every stored order result for the account, exactly as it was first returned."""
        with db.read_connection(self.database_path, operation="order_history") as connection:
            experiment = self._experiment(connection, experiment_id)
            account = self._load(connection, experiment_id, account_id)
            rows = connection.execute(
                "SELECT result FROM acct_orders WHERE account_id = ?", (str(account_id),)
            ).fetchall()
        results = [order_result_adapter.validate_json(row["result"]) for row in rows]
        return self._page_scope(experiment, account), results

    def portfolio(self, experiment_id: UUID, account_id: UUID) -> PortfolioSnapshot:
        with db.read_connection(self.database_path, operation="portfolio") as connection:
            experiment = self._experiment(connection, experiment_id)
            account = self._load(connection, experiment_id, account_id)
        at = account.closed_at or experiment.cutoff_at
        return self._value(experiment, self._snapshot(account, at), at)

    def account_history(
        self, experiment_id: UUID, account_id: UUID
    ) -> tuple[PageScope, list[AccountSnapshot]]:
        """The account as created (state 0), then as it stood after each fill."""
        with db.read_connection(self.database_path, operation="account_history") as connection:
            experiment = self._experiment(connection, experiment_id)
            account = self._load(connection, experiment_id, account_id)
            states = self._states(connection, account_id)
        return self._page_scope(experiment, account), states

    def portfolio_history(
        self, experiment_id: UUID, account_id: UUID
    ) -> tuple[PageScope, list[PortfolioSnapshot] | None]:
        """One valuation per cutoff the experiment has had, from the account's creation to its
        close or the current cutoff. None when the experiment's cutoff record is incomplete.

        Each valuation uses the account as it stood after every fill at or before that cutoff,
        marked with the close available at that cutoff (value-v1).
        """
        experiment, cutoffs = self.clock.cutoff_history(experiment_id)
        with db.read_connection(self.database_path, operation="portfolio_history") as connection:
            account = self._load(connection, experiment_id, account_id)
            states = self._states(connection, account_id)
        scope = self._page_scope(experiment, account)
        if cutoffs is None:
            return scope, None
        end = account.closed_at or experiment.cutoff_at
        valuations = []
        for cutoff in cutoffs:
            if not states[0].simulated_at <= cutoff <= end:
                continue
            state = [s for s in states if s.simulated_at <= cutoff][-1]
            valuations.append(self._value(experiment, state, cutoff))
        return scope, valuations

    @staticmethod
    def _states(connection: sqlite3.Connection, account_id: UUID) -> list[AccountSnapshot]:
        created = connection.execute(
            "SELECT created_response FROM acct_accounts WHERE account_id = ?", (str(account_id),)
        ).fetchone()
        fills = connection.execute(
            "SELECT result FROM acct_orders WHERE account_id = ? AND status = 'filled' "
            "ORDER BY rowid",
            (str(account_id),),
        ).fetchall()
        return [
            AccountSnapshot.model_validate_json(created["created_response"]),
            *(FilledOrder.model_validate_json(row["result"]).account for row in fills),
        ]

    def _value(
        self, experiment: Experiment, state: AccountSnapshot, at: datetime
    ) -> PortfolioSnapshot:
        """value-v1: each holding marked with the close available at `at`."""
        rule = rule_for(experiment.execution_rule_version)
        prices = self.prices_for(experiment.data_version)
        marked = []
        for holding in state.holdings:
            try:
                mark = prices.price_at(holding.symbol, at)
            except MissingData:
                raise db.MarketError(
                    409, ErrorCode.DATA_UNAVAILABLE, f"No mark for {holding.symbol} at {at}"
                ) from None
            marked.append((holding, mark))
        return PortfolioSnapshot(
            account_id=state.account_id,
            experiment_id=experiment.experiment_id,
            simulated_at=at,
            state_version=state.state_version,
            cash=state.cash,
            holdings=tuple(
                MarkedHolding(
                    symbol=holding.symbol,
                    quantity=holding.quantity,
                    unit_mark=mark.price,
                    mark_observed_at=mark.observed_at,
                    mark_available_at=mark.available_at,
                )
                for holding, mark in marked
            ),
            portfolio_value=state.cash
            + sum(
                (rule.market_value(int(h.quantity), mark.price) for h, mark in marked),
                Decimal(0),
            ),
            valuation_rule_version=rule.valuation_rule_version,
            source=prices.price_source,
            data_version=experiment.data_version,
        )

    def _execute(
        self,
        connection: sqlite3.Connection,
        experiment: Experiment,
        rule: ExecutionRule,
        account: _Account,
        order: OrderRequest,
        quantity: int,
        now: datetime,
        order_id: UUID,
    ) -> FilledOrder | RejectedOrder:
        def reject(code: ErrorCode, message: str) -> RejectedOrder:
            return RejectedOrder(
                order_id=order_id,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                rejected_at=now,
                error=ExecutionErrorDetail(code=code, message=message),
                account=self._snapshot(account, now),
            )

        prices = self.prices_for(experiment.data_version)
        session = latest_session(prices, now)
        if session is None:
            return reject(ErrorCode.MARKET_CLOSED, "No trading session has closed by the cutoff")
        try:
            price = prices.price_at(order.symbol, now)
        except MissingData:
            return reject(ErrorCode.DATA_UNAVAILABLE, f"No price for {order.symbol}")
        if price.available_at > now:
            raise db.MarketError(500, ErrorCode.INTERNAL_ERROR, "Price source returned future data")
        if price.observed_at != session.close_at:
            # exec-v1 fills only at the latest session's close. A symbol that has no bar there
            # (delisted, acquired, halted) must not fill at an older, dead price.
            return reject(ErrorCode.DATA_UNAVAILABLE, f"No {session.day} close for {order.symbol}")
        notional = to_cents(rule.notional(quantity, price.price))
        holdings = dict(account.holdings)
        if order.side is OrderSide.BUY:
            if notional > account.cash_cents:
                return reject(ErrorCode.INSUFFICIENT_CASH, "Not enough cash for this order")
            cash = account.cash_cents - notional
            holdings[order.symbol] = holdings.get(order.symbol, 0) + quantity
        else:
            if holdings.get(order.symbol, 0) < quantity:
                return reject(ErrorCode.INSUFFICIENT_HOLDINGS, "Not enough shares for this order")
            cash = account.cash_cents + notional
            holdings[order.symbol] -= quantity
            if holdings[order.symbol] == 0:
                del holdings[order.symbol]
        state_version = account.state_version + 1
        connection.execute(
            "UPDATE acct_accounts SET cash_cents = ?, state_version = ? WHERE account_id = ?",
            (cash, state_version, str(account.account_id)),
        )
        connection.execute(
            "DELETE FROM acct_holdings WHERE account_id = ?", (str(account.account_id),)
        )
        connection.executemany(
            "INSERT INTO acct_holdings VALUES (?, ?, ?)",
            [(str(account.account_id), symbol, qty) for symbol, qty in holdings.items()],
        )
        updated = replace(account, cash_cents=cash, state_version=state_version, holdings=holdings)
        return FilledOrder(
            order_id=order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            unit_price=price.price,
            fee=Decimal(0),
            executed_at=now,
            price_observed_at=price.observed_at,
            price_available_at=price.available_at,
            price_source=prices.price_source,
            data_version=experiment.data_version,
            execution_rule_version=rule.version,
            account=self._snapshot(updated, now),
        )

    @staticmethod
    def _record_fill(
        connection: sqlite3.Connection, fill: FilledOrder, rule: ExecutionRule
    ) -> None:
        quantity = int(fill.quantity)
        connection.execute(
            "INSERT INTO acct_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(fill.order_id),
                str(fill.account.account_id),
                fill.symbol,
                fill.side.value,
                quantity,
                str(fill.unit_price),
                to_cents(rule.notional(quantity, fill.unit_price)),
                to_cents(fill.account.cash),
                fill.account.state_version,
                db.format_time(fill.executed_at),
                db.format_time(fill.price_observed_at),
                db.format_time(fill.price_available_at),
                fill.price_source,
                fill.data_version,
                fill.execution_rule_version,
            ),
        )

    @staticmethod
    def _experiment(connection: sqlite3.Connection, experiment_id: UUID) -> Experiment:
        try:
            return load_experiment(connection, experiment_id)
        except UnknownExperiment:
            raise db.MarketError(404, ErrorCode.NOT_FOUND, "Unknown experiment") from None

    @staticmethod
    def _running_experiment(connection: sqlite3.Connection, experiment_id: UUID) -> Experiment:
        try:
            return load_experiment(connection, experiment_id)
        except UnknownExperiment:
            raise db.MarketError(
                409, ErrorCode.EXPERIMENT_NOT_RUNNING, "Set the experiment's cutoff first"
            ) from None

    @staticmethod
    def _load(connection: sqlite3.Connection, experiment_id: UUID, account_id: UUID) -> _Account:
        row = connection.execute(
            "SELECT * FROM acct_accounts WHERE account_id = ? AND experiment_id = ?",
            (str(account_id), str(experiment_id)),
        ).fetchone()
        if row is None:
            raise db.MarketError(404, ErrorCode.NOT_FOUND, "Unknown account")
        holdings = connection.execute(
            "SELECT symbol, quantity FROM acct_holdings WHERE account_id = ?", (str(account_id),)
        ).fetchall()
        return _Account(
            account_id=account_id,
            experiment_id=experiment_id,
            agent_id=UUID(row["agent_id"]),
            strategy_version_id=UUID(row["strategy_version_id"]),
            cash_cents=row["cash_cents"],
            state_version=row["state_version"],
            closed_at=db.parse_time(row["closed_at"]) if row["closed_at"] else None,
            holdings={h["symbol"]: h["quantity"] for h in holdings},
        )

    @staticmethod
    def _next_order_id(connection: sqlite3.Connection) -> UUID:
        """A UUID whose string sorts in placement order: the first 16 hex digits are the order's
        sequence number. Call it inside the order's write transaction, which serializes writers.

        Order history sorts by (simulated_at, order_id), so an order placed while a client is
        paging always sorts after the orders already returned at that cutoff.
        """
        (last,) = connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM acct_orders").fetchone()
        return UUID(hex=f"{last + 1:016x}{secrets.token_hex(8)}")

    @staticmethod
    def _page_scope(experiment: Experiment, account: _Account) -> PageScope:
        return PageScope(
            experiment_id=experiment.experiment_id,
            account_id=account.account_id,
            agent_id=account.agent_id,
            strategy_version_id=account.strategy_version_id,
            cutoff_at=experiment.cutoff_at,
            data_version=experiment.data_version,
        )

    @staticmethod
    def _snapshot(account: _Account, at: datetime) -> AccountSnapshot:
        return AccountSnapshot(
            account_id=account.account_id,
            agent_id=account.agent_id,
            experiment_id=account.experiment_id,
            strategy_version_id=account.strategy_version_id,
            simulated_at=at,
            state_version=account.state_version,
            cash=from_cents(account.cash_cents),
            holdings=tuple(
                Holding(symbol=symbol, quantity=Decimal(quantity))
                for symbol, quantity in sorted(account.holdings.items())
            ),
        )

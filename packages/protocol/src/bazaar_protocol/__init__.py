"""Shared wire contracts; market authorization and accounting remain server responsibilities."""

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)


def exact_amount_input(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        # Pydantic wraps ValueError in ValidationError; TypeError would escape validation.
        raise ValueError("amounts must be decimal strings, integers or Decimal values")  # noqa: TRY004
    return value


ExactAmount = Annotated[
    Decimal, BeforeValidator(exact_amount_input, json_schema_input_type=str | int)
]
NonNegativeAmount = Annotated[ExactAmount, Field(ge=0, allow_inf_nan=False)]
PositiveAmount = Annotated[ExactAmount, Field(gt=0, allow_inf_nan=False)]
Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")]
Version = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
Cursor = Annotated[str, Field(min_length=1, max_length=2048)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator(
        "simulated_at",
        "executed_at",
        "rejected_at",
        "price_observed_at",
        "price_available_at",
        "observed_at",
        "available_at",
        "start_at",
        "end_at",
        "cutoff_at",
        "mark_observed_at",
        "mark_available_at",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def reject_epoch_timestamps(cls, value: object, info: ValidationInfo) -> object:
        assert info.field_name is not None
        if cls.model_fields[info.field_name].annotation is AwareDatetime:
            if not isinstance(value, (str, datetime)):
                raise ValueError("timestamps must be ISO 8601 strings or datetime values")
            if isinstance(value, str):
                datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def require_utc(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must use UTC")
        return self


class ExperimentContext(WireModel):
    """Trusted server/runner context, never accepted as agent-supplied approval."""

    experiment_id: UUID
    agent_id: UUID
    account_id: UUID
    strategy_version_id: UUID
    approval_id: UUID
    simulated_at: AwareDatetime
    event_sequence: Annotated[int, Field(ge=0, strict=True)]
    data_version: Version
    execution_rule_version: Version


class Holding(WireModel):
    symbol: Symbol
    quantity: PositiveAmount


class AccountSnapshot(WireModel):
    account_id: UUID
    agent_id: UUID
    experiment_id: UUID
    strategy_version_id: UUID
    simulated_at: AwareDatetime
    state_version: Annotated[int, Field(ge=0, strict=True)]
    currency: Literal["USD"] = "USD"
    cash: NonNegativeAmount
    holdings: tuple[Holding, ...] = ()

    @model_validator(mode="after")
    def unique_symbols(self) -> Self:
        if len({h.symbol for h in self.holdings}) != len(self.holdings):
            raise ValueError("holdings must contain each symbol at most once")
        return self


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderRequest(WireModel):
    client_order_id: UUID
    symbol: Symbol
    side: OrderSide
    quantity: PositiveAmount


class ErrorCode(StrEnum):
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_HOLDINGS = "insufficient_holdings"
    MARKET_CLOSED = "market_closed"
    DATA_UNAVAILABLE = "data_unavailable"
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    EXPERIMENT_NOT_APPROVED = "experiment_not_approved"
    EXPERIMENT_NOT_RUNNING = "experiment_not_running"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"


class ErrorDetail(WireModel):
    code: ErrorCode
    message: Annotated[str, Field(min_length=1)]
    retryable: bool = False


class ExecutionErrorDetail(ErrorDetail):
    code: Literal[
        ErrorCode.INSUFFICIENT_CASH,
        ErrorCode.INSUFFICIENT_HOLDINGS,
        ErrorCode.MARKET_CLOSED,
        ErrorCode.DATA_UNAVAILABLE,
    ]


class ApiError(WireModel):
    error: ErrorDetail


class FilledOrder(WireModel):
    status: Literal["filled"] = "filled"
    order_id: UUID
    client_order_id: UUID
    symbol: Symbol
    side: OrderSide
    quantity: PositiveAmount
    unit_price: PositiveAmount
    fee: NonNegativeAmount
    executed_at: AwareDatetime
    price_observed_at: AwareDatetime
    price_available_at: AwareDatetime
    price_source: Version
    data_version: Version
    execution_rule_version: Version
    account: AccountSnapshot

    @model_validator(mode="after")
    def consistent_time(self) -> Self:
        if not self.price_observed_at <= self.price_available_at <= self.executed_at:
            raise ValueError("fill price must be observed and available by execution")
        if self.account.simulated_at != self.executed_at:
            raise ValueError("account must be the post-fill snapshot at execution time")
        return self


class RejectedOrder(WireModel):
    status: Literal["rejected"] = "rejected"
    order_id: UUID
    client_order_id: UUID
    symbol: Symbol
    side: OrderSide
    quantity: PositiveAmount
    rejected_at: AwareDatetime
    error: ExecutionErrorDetail
    account: AccountSnapshot

    @model_validator(mode="after")
    def consistent_time(self) -> Self:
        if self.account.simulated_at != self.rejected_at:
            raise ValueError("account must be the unchanged snapshot at rejection time")
        return self


OrderResult = Annotated[FilledOrder | RejectedOrder, Field(discriminator="status")]
order_result_adapter = TypeAdapter(OrderResult)


class PriceObservation(WireModel):
    observed_at: AwareDatetime
    available_at: AwareDatetime
    price: PositiveAmount

    @model_validator(mode="after")
    def availability_order(self) -> Self:
        if self.available_at < self.observed_at:
            raise ValueError("a price cannot be available before its observation")
        return self


class PriceHistoryRequest(WireModel):
    symbol: Symbol
    start_at: AwareDatetime
    end_at: AwareDatetime
    limit: Annotated[int, Field(ge=1, le=1000, strict=True)] = 100
    cursor: Cursor | None = None

    @field_validator("limit", mode="before")
    @classmethod
    def parse_query_limit(cls, value: object) -> object:
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        return value

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        return self


class PriceHistory(WireModel):
    experiment_id: UUID
    symbol: Symbol
    cutoff_at: AwareDatetime
    source: Version
    data_version: Version
    observations: tuple[PriceObservation, ...]
    next_cursor: Cursor | None = None

    @model_validator(mode="after")
    def point_in_time(self) -> Self:
        for point in self.observations:
            if point.available_at > self.cutoff_at or point.observed_at > self.cutoff_at:
                raise ValueError("price history must not expose future information")
        times = [point.observed_at for point in self.observations]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("price observations must be strictly ordered")
        return self


class MarkedHolding(Holding):
    unit_mark: NonNegativeAmount
    mark_observed_at: AwareDatetime
    mark_available_at: AwareDatetime

    @model_validator(mode="after")
    def availability_order(self) -> Self:
        if self.mark_available_at < self.mark_observed_at:
            raise ValueError("a mark cannot be available before its observation")
        return self


class PortfolioSnapshot(WireModel):
    account_id: UUID
    experiment_id: UUID
    simulated_at: AwareDatetime
    state_version: Annotated[int, Field(ge=0, strict=True)]
    currency: Literal["USD"] = "USD"
    cash: NonNegativeAmount
    holdings: tuple[MarkedHolding, ...] = ()
    portfolio_value: NonNegativeAmount
    valuation_rule_version: Version
    source: Version
    data_version: Version

    @model_validator(mode="after")
    def consistent_valuation(self) -> Self:
        if len({h.symbol for h in self.holdings}) != len(self.holdings):
            raise ValueError("holdings must contain each symbol at most once")
        if any(h.mark_available_at > self.simulated_at for h in self.holdings):
            raise ValueError("portfolio marks must not expose future information")
        return self

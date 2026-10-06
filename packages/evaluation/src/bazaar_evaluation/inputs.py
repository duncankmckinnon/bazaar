"""Evaluator input evidence.

Corporate actions, the full price timeline and inference spend are local models: the market
ledger does not publish them yet. Adapters will map the market's shapes onto these.
"""

from typing import Annotated, Literal, Self

from bazaar_protocol import (
    AccountSnapshot,
    ExperimentContext,
    NonNegativeAmount,
    OrderResult,
    PositiveAmount,
    PriceObservation,
    Symbol,
    Version,
)
from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from bazaar_evaluation._base import EvaluationModel


class _CorporateAction(EvaluationModel):
    effective_at: AwareDatetime
    source: Version
    data_version: Version


class Split(_CorporateAction):
    kind: Literal["split"] = "split"
    symbol: Symbol
    ratio: PositiveAmount  # new shares per old share


class CashDividend(_CorporateAction):
    kind: Literal["cash_dividend"] = "cash_dividend"
    symbol: Symbol
    amount_per_share: PositiveAmount


class SymbolChange(_CorporateAction):
    kind: Literal["symbol_change"] = "symbol_change"
    old_symbol: Symbol
    new_symbol: Symbol

    @model_validator(mode="after")
    def symbols_differ(self) -> Self:
        if self.old_symbol == self.new_symbol:
            raise ValueError("old_symbol and new_symbol must differ")
        return self


class CashAcquisition(_CorporateAction):
    """The position closes at a fixed cash price per share."""

    kind: Literal["cash_acquisition"] = "cash_acquisition"
    symbol: Symbol
    cash_per_share: NonNegativeAmount


CorporateAction = Annotated[
    Split | CashDividend | SymbolChange | CashAcquisition, Field(discriminator="kind")
]
corporate_action_adapter = TypeAdapter(CorporateAction)


class PriceSeries(EvaluationModel):
    symbol: Symbol
    observations: tuple[PriceObservation, ...]


class EvaluationTimeline(EvaluationModel):
    """The full declared timeline, including prices after each decision. Evaluator-only."""

    start_at: AwareDatetime
    end_at: AwareDatetime
    source: Version
    data_version: Version
    series: tuple[PriceSeries, ...] = ()

    @model_validator(mode="after")
    def observations_inside_window(self) -> Self:
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        if len({s.symbol for s in self.series}) != len(self.series):
            raise ValueError("series must contain each symbol at most once")
        for s in self.series:
            times = [point.observed_at for point in s.observations]
            if any(not self.start_at <= t <= self.end_at for t in times):
                raise ValueError(f"{s.symbol} has an observation outside the declared window")
            if times != sorted(times) or len(set(times)) != len(times):
                raise ValueError(f"{s.symbol} observations must be strictly ordered")
        return self


class InferenceSpend(EvaluationModel):
    """LLM cost in USD, kept apart from the account's market currency."""

    usd: NonNegativeAmount
    input_tokens: Annotated[int, Field(ge=0, strict=True)]
    output_tokens: Annotated[int, Field(ge=0, strict=True)]
    incurred_at: AwareDatetime


class RunEvidence(EvaluationModel):
    """One run's ledger evidence under its trusted experiment context."""

    context: ExperimentContext
    opening_account: AccountSnapshot
    orders: tuple[OrderResult, ...] = ()
    corporate_actions: tuple[CorporateAction, ...] = ()
    inference_spend: tuple[InferenceSpend, ...] = ()

    @model_validator(mode="after")
    def orders_belong_to_run_in_time_order(self) -> Self:
        opening = self.opening_account
        for field in ("experiment_id", "account_id", "agent_id", "strategy_version_id"):
            if getattr(self.context, field) != getattr(opening, field):
                raise ValueError(f"context {field} must match the opening account")
        for order in self.orders:
            if (order.account.account_id, order.account.experiment_id) != (
                opening.account_id,
                opening.experiment_id,
            ):
                raise ValueError("every order must belong to the opening account's experiment")
        times = [opening.simulated_at, *(o.account.simulated_at for o in self.orders)]
        if times != sorted(times):
            raise ValueError("orders must be ordered by time, after the opening account")
        return self

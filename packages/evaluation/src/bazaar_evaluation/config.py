from datetime import timedelta
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Literal

from bazaar_protocol import PositiveAmount, Version
from pydantic import Field, field_validator

from bazaar_evaluation._base import EvaluationModel


class CashRoundingRule(EvaluationModel):
    """How a market rule rounds an amount: a fill's cash delta or a holding's marked value.

    `quantum=None` means exact.
    """

    quantum: PositiveAmount | None
    rounding: Literal["half_even", "half_up"] = "half_even"

    def apply(self, amount: Decimal) -> Decimal:
        if self.quantum is None:
            return amount
        mode = ROUND_HALF_EVEN if self.rounding == "half_even" else ROUND_HALF_UP
        return (amount / self.quantum).quantize(Decimal(1), rounding=mode) * self.quantum


class EvaluatorConfig(EvaluationModel):
    """Versioned evaluator settings; every result records `evaluator_version`."""

    evaluator_version: Version
    horizons: tuple[timedelta, ...] = ()
    lot_method: Literal["fifo", "specific_lot"] = "fifo"
    execution_rules: dict[Version, CashRoundingRule] = Field(default_factory=dict)
    valuation_rules: dict[Version, CashRoundingRule] = Field(default_factory=dict)

    @field_validator("horizons")
    @classmethod
    def positive_unique_horizons(cls, value: tuple[timedelta, ...]) -> tuple[timedelta, ...]:
        if any(h <= timedelta(0) for h in value):
            raise ValueError("horizons must be positive")
        if len(set(value)) != len(value):
            raise ValueError("horizons must be unique")
        return value


_CENTS = CashRoundingRule(quantum=Decimal("0.01"), rounding="half_even")

# The market's binding demo rules: exec-v1 rounds each fill's notional, value-v1 each holding.
DEMO_CONFIG = EvaluatorConfig(
    evaluator_version="evals-demo-v1",
    execution_rules={"exec-v1": _CENTS},
    valuation_rules={"value-v1": _CENTS},
)

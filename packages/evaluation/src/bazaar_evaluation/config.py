from datetime import timedelta
from typing import Literal

from bazaar_protocol import PositiveAmount, Version
from pydantic import Field, field_validator

from bazaar_evaluation._base import EvaluationModel


class CashRoundingRule(EvaluationModel):
    """How an execution rule rounds each fill's cash delta (notional plus fee).

    `quantum=None` means exact. A placeholder until the market rig publishes its own rule.
    """

    quantum: PositiveAmount | None
    rounding: Literal["half_even", "half_up"] = "half_even"


class EvaluatorConfig(EvaluationModel):
    """Versioned evaluator settings; every result records `evaluator_version`."""

    evaluator_version: Version
    horizons: tuple[timedelta, ...] = ()
    lot_method: Literal["fifo", "specific_lot"] = "fifo"
    execution_rules: dict[Version, CashRoundingRule] = Field(default_factory=dict)

    @field_validator("horizons")
    @classmethod
    def positive_unique_horizons(cls, value: tuple[timedelta, ...]) -> tuple[timedelta, ...]:
        if any(h <= timedelta(0) for h in value):
            raise ValueError("horizons must be positive")
        if len(set(value)) != len(value):
            raise ValueError("horizons must be unique")
        return value

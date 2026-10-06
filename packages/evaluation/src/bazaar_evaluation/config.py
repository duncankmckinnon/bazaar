from datetime import timedelta
from typing import Literal

from bazaar_protocol import Symbol, Version
from pydantic import field_validator

from bazaar_evaluation._base import EvaluationModel


class EvaluatorConfig(EvaluationModel):
    """Versioned evaluator settings; every result records `evaluator_version`."""

    evaluator_version: Version
    horizons: tuple[timedelta, ...] = ()
    lot_method: Literal["fifo", "specific_lot"] = "fifo"
    baseline_symbols: tuple[Symbol, ...] = ()

    @field_validator("horizons")
    @classmethod
    def positive_horizons(cls, value: tuple[timedelta, ...]) -> tuple[timedelta, ...]:
        if any(h <= timedelta(0) for h in value):
            raise ValueError("horizons must be positive")
        return value

    @field_validator("baseline_symbols")
    @classmethod
    def unique_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("baseline symbols must be unique")
        return value

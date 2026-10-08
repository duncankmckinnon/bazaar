from datetime import datetime

from bazaar_protocol import WireModel
from pydantic import AwareDatetime, ValidationInfo, field_validator


class EvaluationModel(WireModel):
    """Frozen, extra-forbidding, UTC-only like the wire models, for evaluator-local timestamps."""

    @field_validator("effective_at", "incurred_at", mode="before", check_fields=False)
    @classmethod
    def reject_epoch_local_timestamps(cls, value: object, info: ValidationInfo) -> object:
        assert info.field_name is not None
        if cls.model_fields[info.field_name].annotation is AwareDatetime:
            if not isinstance(value, (str, datetime)):
                raise ValueError("timestamps must be ISO 8601 strings or datetime values")
            if isinstance(value, str):
                datetime.fromisoformat(value)
        return value

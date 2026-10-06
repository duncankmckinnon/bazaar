"""Named-agent and immutable strategy registration contracts."""

import re
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from bazaar_protocol import WireModel

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
Reference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
Harness = Literal["single_shot", "orchestrated", "monty", "research"]
Tool = Literal["account", "market_history", "orders", "news", "reports", "monty", "private_history"]


class StrategyDefinition(WireModel):
    harness: Harness = "single_shot"
    model_ref: Reference
    instructions: Text
    tools: tuple[Tool, ...] = ("account", "market_history", "orders")
    artifact_ref: Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^\S+$")] | None = (
        None
    )

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: tuple[Tool, ...]) -> tuple[Tool, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tool references must be unique")
        return tuple(sorted(value))


class CreateStrategyRequest(WireModel):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")]
    description: Annotated[str, Field(max_length=2000)] = ""
    definition: StrategyDefinition
    parent_version_id: UUID | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            return re.sub(r"[\s_]+", "-", value.strip().lower())
        return value


class CreateVersionRequest(WireModel):
    definition: StrategyDefinition
    hypothesis: Annotated[str, Field(max_length=2000)] = ""
    parent_version_id: UUID | None = None


class AgentRecord(WireModel):
    agent_id: UUID
    name: str
    strategy_id: UUID
    created_at: AwareDatetime
    created_by: str


class StrategyRecord(WireModel):
    strategy_id: UUID
    agent_id: UUID
    description: str
    created_at: AwareDatetime
    created_by: str
    latest_version_id: UUID
    version_count: Annotated[int, Field(ge=1)]


class StrategyVersion(WireModel):
    version_id: UUID
    strategy_id: UUID
    version: Annotated[int, Field(ge=1)]
    definition: StrategyDefinition
    parent_version_id: UUID | None
    hypothesis: str
    definition_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    created_at: AwareDatetime
    created_by: str


class StrategyRegistration(WireModel):
    status: Literal["proposed"] = "proposed"
    agent: AgentRecord
    strategy: StrategyRecord
    version: StrategyVersion


T = TypeVar("T")


class Page[T](WireModel):
    items: tuple[T, ...]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]

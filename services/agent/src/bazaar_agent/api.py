"""Local strategy-registry API, separate from trading-agent execution and the market."""

import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import logfire
import uvicorn
from bazaar_protocol import ApiError, ErrorCode, ErrorDetail
from bazaar_protocol.registry import (
    AgentRecord,
    CreateStrategyRequest,
    CreateVersionRequest,
    Page,
    StrategyRecord,
    StrategyRegistration,
    StrategyVersion,
)
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from bazaar_agent.registry_store import RegistryError, RegistryStore
from bazaar_agent.telemetry import configure_telemetry

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
RequestKey = Annotated[UUID, Header(alias="Idempotency-Key")]


def get_registry(request: Request) -> RegistryStore:
    return request.app.state.registry


Registry = Annotated[RegistryStore, Depends(get_registry)]


def error_response(status: int, code: ErrorCode, message: str) -> JSONResponse:
    error = ApiError(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=error.model_dump(mode="json"))


def create_app(
    database_path: Path | str | None = None,
    model_refs: frozenset[str] | None = None,
    actor_label: str = "local-api",
) -> FastAPI:
    """Create a standalone agent-side registry. No auth or experiment execution is included."""
    configure_telemetry()
    path = Path(database_path or os.getenv("BAZAAR_REGISTRY_DB_PATH", "data/registry.sqlite3"))
    refs = (
        model_refs
        if model_refs is not None
        else frozenset(
            ref.strip()
            for ref in os.getenv("BAZAAR_REGISTRY_MODEL_REFS", "test").split(",")
            if ref.strip()
        )
    )
    store = RegistryStore(path.resolve(), refs, actor_label)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        store.initialize()
        yield

    application = FastAPI(
        title="Bazaar agent registry",
        lifespan=lifespan,
        responses={code: {"model": ApiError} for code in (404, 409, 422, 500)},
    )
    application.state.registry = store

    @application.exception_handler(RegistryError)
    async def registry_error(request: Request, error: RegistryError) -> JSONResponse:
        logfire.warn(
            "Registry request rejected", error_code=error.code.value, status_code=error.status_code
        )
        return error_response(error.status_code, error.code, error.message)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Do not echo invalid payloads, which may contain accidentally submitted credentials.
        logfire.warn("Invalid registry request", status_code=422)
        return error_response(422, ErrorCode.INVALID_REQUEST, "Invalid request")

    @application.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error: sqlite3.Error) -> JSONResponse:
        logfire.error("Registry storage error", status_code=500)
        return error_response(500, ErrorCode.INTERNAL_ERROR, "Registry storage error")

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/strategies", response_model=StrategyRegistration, status_code=201)
    def register(
        request: CreateStrategyRequest, registry: Registry, key: RequestKey
    ) -> StrategyRegistration:
        return registry.register(request, key)

    @application.get("/strategies", response_model=Page[StrategyRecord])
    def strategies(
        registry: Registry, limit: Limit = 20, offset: Offset = 0
    ) -> Page[StrategyRecord]:
        return registry.list_strategies(limit, offset)

    @application.get("/strategies/{strategy_id}", response_model=StrategyRecord)
    def strategy(strategy_id: UUID, registry: Registry) -> StrategyRecord:
        return registry.get_strategy(strategy_id)

    @application.post(
        "/strategies/{strategy_id}/versions", response_model=StrategyVersion, status_code=201
    )
    def add_version(
        strategy_id: UUID, request: CreateVersionRequest, registry: Registry, key: RequestKey
    ) -> StrategyVersion:
        return registry.add_version(strategy_id, request, key)

    @application.get("/strategies/{strategy_id}/versions", response_model=Page[StrategyVersion])
    def versions(
        strategy_id: UUID, registry: Registry, limit: Limit = 20, offset: Offset = 0
    ) -> Page[StrategyVersion]:
        return registry.list_versions(strategy_id, limit, offset)

    @application.get(
        "/strategies/{strategy_id}/versions/{version_id}", response_model=StrategyVersion
    )
    def version(strategy_id: UUID, version_id: UUID, registry: Registry) -> StrategyVersion:
        return registry.get_version(strategy_id, version_id)

    @application.get("/agents", response_model=Page[AgentRecord])
    def agents(registry: Registry, limit: Limit = 20, offset: Offset = 0) -> Page[AgentRecord]:
        return registry.list_agents(limit, offset)

    @application.get("/agents/{agent_id}", response_model=AgentRecord)
    def agent(agent_id: UUID, registry: Registry) -> AgentRecord:
        return registry.get_agent(agent_id)

    logfire.instrument_fastapi(
        application,
        capture_headers=False,
        request_attributes_mapper=lambda request, attributes: None,
        excluded_urls="/health,/docs,/openapi.json",
    )
    return application


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("BAZAAR_API_HOST", "127.0.0.1"), port=8001)

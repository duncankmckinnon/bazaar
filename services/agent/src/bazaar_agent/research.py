"""Scoped async tools. Context comes from the runner, never from trading inputs.

Validation is defense in depth, NOT server authorization. No automatic order retries,
state cache, external search, database access or unrestricted private-history query.
"""

from datetime import date, datetime
from typing import Literal, Protocol, TypeVar

import httpx
import logfire
from bazaar_protocol import (
    AccountSnapshot,
    ExperimentContext,
    FilledOrder,
    OrderRequest,
    OrderResult,
    PortfolioSnapshot,
    PriceHistory,
    PriceHistoryRequest,
    RejectedOrder,
    Symbol,
    WireModel,
    order_result_adapter,
)
from bazaar_protocol.research import (
    AccountHistoryPage,
    CompanyFiling,
    FilingPage,
    HistoryPage,
    HistoryRequest,
    NewsPage,
    OrderHistoryPage,
    PortfolioHistoryPage,
    PrivateHistoryPage,
    PrivateRecord,
    Provenance,
    ResearchRequest,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

T = TypeVar("T")


class ToolError(WireModel):
    code: Literal[
        "invalid_request",
        "invalid_response",
        "unsupported",
        "missing_data",
        "unauthorized",
        "conflict",
        "network",
        "server_error",
    ]
    message: str
    retryable: bool = False


class ToolResult[T](WireModel):
    data: T | None = None
    error: ToolError | None = None


class FiscalCycle(WireModel):
    symbol: Symbol
    start: date


class ResearchContext(WireModel):
    experiment: ExperimentContext
    # Runner-owned company cycle boundaries; absence means unsupported, not a guessed cycle.
    cycles: tuple[FiscalCycle, ...] = ()


class PrivateHistoryReader(Protocol):
    """SDK-bound in #23; implementation must enforce scope server-side too."""

    async def read(
        self, context: ExperimentContext, request: HistoryRequest
    ) -> PrivateHistoryPage: ...


class PrivateHistoryUnavailable(Exception):
    """Safe sentinel; never forward adapter exception messages."""


class UnavailablePrivateHistory:
    async def read(self, context: ExperimentContext, request: HistoryRequest) -> PrivateHistoryPage:
        raise PrivateHistoryUnavailable


class BoundaryError(Exception):
    pass


def require(condition: bool) -> None:
    if not condition:
        raise BoundaryError


class ResearchTools:
    """Methods can be registered/wrapped as PydanticAI tools in #24.

    The caller owns/ closes the AsyncClient. Pass a fixed base_url and authentication
    out of band. Do not instrument bodies/headers. Instances are bound to ONE immutable
    runner context: recreate on clock/strategy change; never inherit cursor state.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        context: ResearchContext,
        private_history: PrivateHistoryReader | None = None,
    ) -> None:
        self._client = client
        self._context = ResearchContext.model_validate_json(context.model_dump_json())
        self._private = private_history or UnavailablePrivateHistory()
        self._cursors: dict[tuple[str, str], str] = {}
        self._cursor_sources: dict[tuple[str, str], str] = {}
        self._cursor_rows: dict[tuple[str, str], tuple[tuple[datetime, str], frozenset[str]]] = {}

    @property
    def _ctx(self) -> ExperimentContext:
        return self._context.experiment

    @property
    def _root(self) -> str:
        return f"/experiments/{self._ctx.experiment_id}"

    @property
    def _account_root(self) -> str:
        return f"{self._root}/accounts/{self._ctx.account_id}"

    def _scope(self, value: BaseModel) -> None:
        for name in ("experiment_id", "account_id", "agent_id", "strategy_version_id"):
            if hasattr(value, name):
                require(getattr(value, name) == getattr(self._ctx, name))

    def _snapshot(self, value: AccountSnapshot | PortfolioSnapshot) -> None:
        self._scope(value)
        require(value.simulated_at <= self._ctx.simulated_at)
        if isinstance(value, PortfolioSnapshot):
            require(value.data_version == self._ctx.data_version)

    @staticmethod
    def _safe_order(value: FilledOrder | RejectedOrder) -> FilledOrder | RejectedOrder:
        if isinstance(value, RejectedOrder):
            return value.model_copy(
                update={"error": value.error.model_copy(update={"message": value.error.code.value})}
            )
        return value

    def _order(self, value: FilledOrder | RejectedOrder) -> None:
        self._snapshot(value.account)
        if isinstance(value, FilledOrder):
            require(value.data_version == self._ctx.data_version)
            require(value.execution_rule_version == self._ctx.execution_rule_version)
        else:
            # Domain error text is untrusted and may contain secrets; use only the enum.
            require(value.error.message == value.error.code.value)

    def _window(self, request: HistoryRequest | PriceHistoryRequest, route: str) -> None:
        require(request.start_at <= request.end_at <= self._ctx.simulated_at)
        if request.cursor is not None:
            require(self._cursors.get((route, request.cursor)) == self._query_key(request))

    @staticmethod
    def _query_key(request: HistoryRequest | PriceHistoryRequest) -> str:
        return request.model_dump_json(exclude={"cursor"})

    def _pagination(
        self,
        request: HistoryRequest | PriceHistoryRequest,
        route: str,
        cursor: str | None,
        count: int,
        keys: list[tuple[datetime, str]],
        source: str,
    ) -> None:
        require(count <= request.limit)
        seen: frozenset[str] = frozenset()
        if request.cursor is not None:
            require(self._cursor_sources[(route, request.cursor)] == source)
            previous, seen = self._cursor_rows[(route, request.cursor)]
            require(not keys or keys[0] > previous)
            require(not seen.intersection(key for _, key in keys))
        if cursor is not None:
            require(count > 0 and cursor != request.cursor)
            token = (route, cursor)
            boundary = (keys[-1], seen.union(key for _, key in keys))
            query = self._query_key(request)
            if token in self._cursors:
                # Stable cursors may be returned by an identical read/retry. Reuse
                # for another boundary, scope/query, or source remains fail-closed.
                require(self._cursors[token] == query)
                require(self._cursor_sources[token] == source)
                require(self._cursor_rows[token] == boundary)
            else:
                self._cursors[token] = query
                self._cursor_sources[token] = source
                self._cursor_rows[token] = boundary

    def _provenance(self, value: Provenance) -> None:
        require(value.data_version == self._ctx.data_version)
        require(
            value.published_at <= value.revised_at <= value.available_at <= self._ctx.simulated_at
        )

    def _page(
        self, value: HistoryPage, request: HistoryRequest | ResearchRequest, route: str
    ) -> None:
        self._scope(value)
        require(value.cutoff_at == self._ctx.simulated_at)
        require(value.start_at == request.start_at and value.end_at == request.end_at)
        require(value.data_version == self._ctx.data_version)
        require(value.coverage == "complete")
        keys: list[tuple[datetime, str]] = []
        for item in value.items:
            if isinstance(item, Provenance):
                self._provenance(item)
                require(item.source == value.source)
                timestamp = item.published_at
                key = item.record_id
                if isinstance(request, ResearchRequest):
                    require(item.symbol == request.symbol)
                if isinstance(item, CompanyFiling):
                    cycles = [c.start for c in self._context.cycles if c.symbol == item.symbol]
                    require(len(cycles) == 1)
                    require(item.fiscal_period_start <= item.fiscal_period_end < cycles[0])
                if isinstance(item, PrivateRecord):
                    require(item.simulated_at <= self._ctx.simulated_at)
                    require(item.event_sequence <= self._ctx.event_sequence)
                    require(item.available_at >= item.simulated_at)
                    timestamp = item.simulated_at
            elif isinstance(item, AccountSnapshot | PortfolioSnapshot):
                self._snapshot(item)
                timestamp = item.simulated_at
                # Marks/clock can advance without an account state change.
                key = f"{item.simulated_at.isoformat()}:{item.state_version:020d}"
            else:
                self._order(item)
                timestamp = item.account.simulated_at
                key = str(item.order_id)
            require(request.start_at <= timestamp <= request.end_at)
            keys.append((timestamp, key))
        require(keys == sorted(keys) and len({k for _, k in keys}) == len(keys))
        self._pagination(request, route, value.next_cursor, len(value.items), keys, value.source)

    async def _call(
        self,
        route: str,
        response_type: type[T] | TypeAdapter[T],
        request: HistoryRequest | PriceHistoryRequest | OrderRequest | None = None,
    ) -> ToolResult[T]:
        # Catch INSIDE span: exceptions/validation payloads must never enter telemetry.
        with logfire.span("research tool", _tags=["research"], _span_name="research.tool"):
            try:
                if request is not None:
                    request = type(request).model_validate_json(request.model_dump_json())
                if isinstance(request, HistoryRequest | PriceHistoryRequest):
                    self._window(request, route)
                    params = request.model_dump(mode="json", exclude={"symbol"}, exclude_none=True)
                else:
                    params = None
                response = await self._client.request(
                    "POST" if isinstance(request, OrderRequest) else "GET",
                    route,
                    params=params,
                    json=request.model_dump(mode="json")
                    if isinstance(request, OrderRequest)
                    else None,
                    follow_redirects=False,
                )
                if response.status_code != 200:
                    code, message = {
                        401: ("unauthorized", "Authentication required"),
                        403: ("unauthorized", "Scope or cutoff denied"),
                        404: ("missing_data", "Resource or endpoint unavailable"),
                        409: ("conflict", "Request conflicts with server state"),
                        422: ("invalid_request", "Server rejected request"),
                        501: ("unsupported", "Endpoint unsupported"),
                    }.get(response.status_code, ("server_error", "Market request failed"))
                    return ToolResult(
                        error=ToolError.model_validate({"code": code, "message": message})
                    )
                adapter = (
                    response_type
                    if isinstance(response_type, TypeAdapter)
                    else TypeAdapter(response_type)
                )
                value = adapter.validate_json(response.content)
                if isinstance(value, HistoryPage):
                    if not isinstance(request, HistoryRequest | ResearchRequest):
                        raise BoundaryError
                    if value.coverage != "complete":
                        return ToolResult(
                            error=ToolError(
                                code="missing_data", message="Archive coverage incomplete"
                            )
                        )
                    if isinstance(value, OrderHistoryPage):
                        value = value.model_copy(
                            update={"items": tuple(self._safe_order(item) for item in value.items)}
                        )
                    self._page(value, request, route)
                elif isinstance(value, PriceHistory):
                    if not isinstance(request, PriceHistoryRequest):
                        raise BoundaryError
                    self._scope(value)
                    require(value.symbol == request.symbol)
                    require(value.cutoff_at == self._ctx.simulated_at)
                    require(value.data_version == self._ctx.data_version)
                    require(
                        all(
                            request.start_at <= p.observed_at <= request.end_at
                            for p in value.observations
                        )
                    )
                    self._pagination(
                        request,
                        route,
                        value.next_cursor,
                        len(value.observations),
                        [(p.observed_at, p.observed_at.isoformat()) for p in value.observations],
                        value.source,
                    )
                elif isinstance(value, FilledOrder | RejectedOrder):
                    # Sanitize domain messages before returning or validating further.
                    value = self._safe_order(value)
                    self._order(value)
                    if not isinstance(request, OrderRequest):
                        raise BoundaryError
                    require(
                        all(
                            getattr(value, n) == getattr(request, n)
                            for n in ("client_order_id", "symbol", "side", "quantity")
                        )
                    )
                elif isinstance(value, AccountSnapshot | PortfolioSnapshot):
                    self._snapshot(value)
                    require(value.simulated_at == self._ctx.simulated_at)
                else:
                    raise BoundaryError
                return ToolResult(data=value)
            except httpx.HTTPError:
                return ToolResult(
                    error=ToolError(
                        code="network",
                        message="Market transport failed; retry orders only with the same ID",
                        retryable=True,
                    )
                )
            except (ValidationError, BoundaryError, ValueError):
                return ToolResult(
                    error=ToolError(
                        code="invalid_response",
                        message="Request or response violates scoped contract",
                    )
                )

    @logfire.instrument("account tool", extract_args=False)
    async def account(self) -> ToolResult[AccountSnapshot]:
        return await self._call(self._account_root, AccountSnapshot)

    @logfire.instrument("portfolio tool", extract_args=False)
    async def portfolio(self) -> ToolResult[PortfolioSnapshot]:
        return await self._call(f"{self._account_root}/portfolio", PortfolioSnapshot)

    @logfire.instrument("order tool", extract_args=False)
    async def place_order(self, request: OrderRequest) -> ToolResult[OrderResult]:
        return await self._call(f"{self._account_root}/orders", order_result_adapter, request)

    @logfire.instrument("prices tool", extract_args=False)
    async def prices(self, request: PriceHistoryRequest) -> ToolResult[PriceHistory]:
        return await self._call(f"{self._root}/prices/{request.symbol}", PriceHistory, request)

    @logfire.instrument("news tool", extract_args=False)
    async def news(self, request: ResearchRequest) -> ToolResult[NewsPage]:
        return await self._call(f"{self._root}/news/{request.symbol}", NewsPage, request)

    @logfire.instrument("filings tool", extract_args=False)
    async def filings(self, request: ResearchRequest) -> ToolResult[FilingPage]:
        cycles = [c for c in self._context.cycles if c.symbol == request.symbol]
        if len(cycles) != 1 or cycles[0].start > self._ctx.simulated_at.date():
            return ToolResult(
                error=ToolError(code="unsupported", message="Trusted fiscal cycle unavailable")
            )
        return await self._call(f"{self._root}/filings/{request.symbol}", FilingPage, request)

    async def account_history(self, request: HistoryRequest) -> ToolResult[AccountHistoryPage]:
        return await self._call(f"{self._account_root}/history", AccountHistoryPage, request)

    async def portfolio_history(self, request: HistoryRequest) -> ToolResult[PortfolioHistoryPage]:
        return await self._call(
            f"{self._account_root}/portfolio/history", PortfolioHistoryPage, request
        )

    async def orders(self, request: HistoryRequest) -> ToolResult[OrderHistoryPage]:
        return await self._call(f"{self._account_root}/orders", OrderHistoryPage, request)

    @logfire.instrument("private history tool", extract_args=False)
    async def private_history(self, request: HistoryRequest) -> ToolResult[PrivateHistoryPage]:
        try:
            request = HistoryRequest.model_validate_json(request.model_dump_json())
            self._window(request, "private")
            value = await self._private.read(self._ctx, request)
            value = PrivateHistoryPage.model_validate_json(value.model_dump_json())
            if value.coverage != "complete":
                return ToolResult(
                    error=ToolError(code="missing_data", message="Private coverage incomplete")
                )
            self._page(value, request, "private")
            return ToolResult(data=value)
        except PrivateHistoryUnavailable:
            return ToolResult(
                error=ToolError(
                    code="unsupported", message="Protected private-history adapter not configured"
                )
            )
        except Exception:  # noqa: BLE001 -- untrusted SDK failures must not leak payloads
            return ToolResult(
                error=ToolError(
                    code="invalid_response", message="Private-history boundary or adapter failure"
                )
            )

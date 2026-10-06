"""MarketPort over the market service's HTTP routes, one adapter per run.

Routes and bodies follow bazaar_market.ledger_api and prices_api. Every call carries the run's
approval id in X-Bazaar-Approval; the market decides on it before writing anything.
"""

import json
import os
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
from bazaar_protocol import (
    AccountSnapshot,
    ApiError,
    ErrorCode,
    ErrorDetail,
    ExperimentContext,
    FilledOrder,
    Holding,
    OrderRequest,
    PortfolioSnapshot,
    PriceHistory,
    PriceObservation,
    RejectedOrder,
    order_result_adapter,
)
from pydantic import ValidationError

from bazaar_runner.clock import utc_z
from bazaar_runner.market import (
    ApprovalDenied,
    FutureData,
    MarketError,
    MissingPrice,
    RunnerUnauthorized,
)

APPROVAL_HEADER = "X-Bazaar-Approval"
# Sent only on the control routes (cutoff, create account, close); never logged or echoed.
RUNNER_TOKEN_HEADER = "X-Bazaar-Runner-Token"
RUNNER_TOKEN_ENV = "BAZAAR_RUNNER_TOKEN"
DEFAULT_BASE_URL = "http://localhost:8000"
# Long enough to reach back over a weekend plus a holiday to the latest close.
PRICE_LOOKBACK = timedelta(days=10)
# The market does not page today; this bounds a server that keeps returning a cursor.
MAX_PRICE_PAGES = 10


class RunnerConfigError(Exception):
    """The runner cannot start: its market credential is not configured."""


def _market_error(response: httpx.Response, *, control: bool) -> MarketError:
    try:
        detail = ApiError.model_validate_json(response.content).error
    except ValidationError:
        detail = ErrorDetail(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"HTTP {response.status_code} without an ApiError body",
        )
    if detail.code is ErrorCode.EXPERIMENT_NOT_APPROVED:
        return ApprovalDenied(detail.message)
    # The approval header is always sent, so a 401 on a control route is the runner token.
    if control and response.status_code == 401:
        return RunnerUnauthorized(detail.message)
    return MarketError(detail)


class HttpMarketPort:
    def __init__(
        self,
        client: httpx.AsyncClient,
        experiment_id: UUID,
        approval_id: UUID,
        runner_token: str,
    ) -> None:
        if not runner_token:
            raise RunnerConfigError("the market runner token is empty")
        self._client = client
        self._experiment_id = experiment_id
        self._headers = {APPROVAL_HEADER: str(approval_id)}
        self._token = runner_token
        self._root = f"/experiments/{experiment_id}"

    @classmethod
    def from_env(
        cls, client: httpx.AsyncClient, experiment_id: UUID, approval_id: UUID
    ) -> "HttpMarketPort":
        token = os.environ.get(RUNNER_TOKEN_ENV)
        if not token:
            raise RunnerConfigError(f"{RUNNER_TOKEN_ENV} is not set")
        return cls(client, experiment_id, approval_id, token)

    def _redact(self, error: MarketError) -> MarketError:
        if self._token not in error.detail.message:
            return error
        message = error.detail.message.replace(self._token, "[redacted]")
        if type(error) is MarketError:
            return MarketError(error.detail.model_copy(update={"message": message}))
        return type(error)(message)

    def _check(self, experiment_id: UUID) -> None:
        if experiment_id != self._experiment_id:
            raise ValueError(f"this adapter is bound to experiment {self._experiment_id}")

    async def _request(self, method: str, path: str, *, control: bool = False, **kwargs) -> bytes:
        headers = self._headers | {RUNNER_TOKEN_HEADER: self._token} if control else self._headers
        # One attempt only: never resend a POST after an ambiguous failure (market-agent API docs).
        try:
            response = await self._client.request(
                method, self._root + path, headers=headers, **kwargs
            )
        except httpx.TransportError as exc:
            detail = ErrorDetail(
                code=ErrorCode.INTERNAL_ERROR, message=f"{type(exc).__name__}: {exc}"
            )
            raise self._redact(MarketError(detail)) from None
        if response.is_error:
            raise self._redact(_market_error(response, control=control))
        return response.content

    async def set_cutoff(
        self,
        experiment_id: UUID,
        cutoff: datetime,
        data_version: str,
        execution_rule_version: str,
    ) -> datetime:
        self._check(experiment_id)
        body = {
            "cutoff": utc_z(cutoff),
            "data_version": data_version,
            "execution_rule_version": execution_rule_version,
        }
        content = await self._request("PUT", "/cutoff", control=True, json=body)
        return datetime.fromisoformat(json.loads(content)["cutoff"])

    async def create_account(
        self,
        experiment_id: UUID,
        agent_id: UUID,
        strategy_version_id: UUID,
        cash: Decimal,
        holdings: Sequence[Holding] = (),
        *,
        request_id: UUID,
    ) -> AccountSnapshot:
        self._check(experiment_id)
        if holdings:
            raise NotImplementedError("the market does not accept seeded holdings yet")
        body = {
            "request_id": str(request_id),
            "agent_id": str(agent_id),
            "strategy_version_id": str(strategy_version_id),
            "cash": str(cash),
        }
        content = await self._request("POST", "/accounts", control=True, json=body)
        return AccountSnapshot.model_validate_json(content)

    async def account(self, ctx: ExperimentContext) -> AccountSnapshot:
        self._check(ctx.experiment_id)
        content = await self._request("GET", f"/accounts/{ctx.account_id}")
        return AccountSnapshot.model_validate_json(content)

    async def submit(
        self, ctx: ExperimentContext, order: OrderRequest
    ) -> FilledOrder | RejectedOrder:
        self._check(ctx.experiment_id)
        content = await self._request(
            "POST", f"/accounts/{ctx.account_id}/orders", json=order.model_dump(mode="json")
        )
        return order_result_adapter.validate_json(content)

    async def portfolio(self, ctx: ExperimentContext) -> PortfolioSnapshot:
        self._check(ctx.experiment_id)
        content = await self._request("GET", f"/accounts/{ctx.account_id}/portfolio")
        return PortfolioSnapshot.model_validate_json(content)

    async def price_at(self, symbol: str, cutoff: datetime) -> PriceObservation:
        params = {
            "start_at": utc_z(cutoff - PRICE_LOOKBACK),
            "end_at": utc_z(cutoff),
            "limit": "100",
        }
        latest: PriceObservation | None = None
        for _ in range(MAX_PRICE_PAGES):
            try:
                content = await self._request("GET", f"/prices/{symbol}", params=params)
            except MarketError as exc:
                # The market's clock is this route's cutoff: 403 forbidden means end_at is past it.
                if exc.detail.code is ErrorCode.FORBIDDEN:
                    raise FutureData(exc.detail.message) from exc
                if exc.detail.code is ErrorCode.NOT_FOUND:
                    raise MissingPrice(symbol, cutoff) from exc
                raise
            page = PriceHistory.model_validate_json(content)
            available = [o for o in page.observations if o.available_at <= cutoff]
            if available:
                latest = available[-1]
            if page.next_cursor is None:
                break
            params = params | {"cursor": page.next_cursor}
        else:
            raise MarketError(
                ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"{symbol} price history did not end after {MAX_PRICE_PAGES} pages",
                )
            )
        if latest is None:
            raise MissingPrice(symbol, cutoff)
        return latest

    async def close_account(self, experiment_id: UUID, account_id: UUID) -> AccountSnapshot:
        self._check(experiment_id)
        content = await self._request("POST", f"/accounts/{account_id}/close", control=True)
        return AccountSnapshot.model_validate_json(content)

"""HTTP routes for the clock, accounts and orders.

Every route requires an approval for the experiment in its path, in the X-Bazaar-Approval header.
A missing header is 401; an approval the GrantChecker does not allow for that experiment is 403
experiment_not_approved. The control routes (cutoff, create account, close) also require the
runner's X-Bazaar-Runner-Token, and refuse every call when no runner token is configured. All of
this is decided before the request body is read, so a refused call writes nothing.
"""

import hmac
import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Protocol
from uuid import UUID

from bazaar_protocol import (
    AccountSnapshot,
    ApiError,
    ErrorCode,
    ErrorDetail,
    FilledOrder,
    NonNegativeAmount,
    OrderRequest,
    PortfolioSnapshot,
    RejectedOrder,
    Version,
    WireModel,
)
from bazaar_protocol.research import OrderHistoryPage
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from bazaar_market.db import MarketError
from bazaar_market.history import PageScope, build_page, parse_history_request
from bazaar_market.ledger import Ledger

logger = logging.getLogger(__name__)

ORDER_HISTORY_SOURCE = "market-ledger-v1"
APPROVAL_HEADER = "X-Bazaar-Approval"
ACCOUNT_HEADER = "X-Bazaar-Account"
RUNNER_TOKEN_HEADER = "X-Bazaar-Runner-Token"


class GrantChecker(Protocol):
    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool: ...


class DenyAllGrants:
    """The default until the approval service (#18) exists."""

    def allows(self, approval_id: UUID, experiment_id: UUID) -> bool:
        return False


class CutoffRequest(WireModel):
    cutoff: AwareDatetime
    data_version: Version | None = None
    execution_rule_version: Version | None = None


class CutoffResponse(WireModel):
    experiment_id: UUID
    cutoff: datetime


class CreateAccountRequest(WireModel):
    request_id: UUID
    agent_id: UUID
    strategy_version_id: UUID
    cash: NonNegativeAmount


def _error(status_code: int, code: ErrorCode, message: str) -> JSONResponse:
    body = ApiError(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def approval_check(grants: GrantChecker) -> Callable[..., UUID]:
    """A dependency for any route with an {experiment_id} path parameter."""

    def require_approval(
        request: Request,
        experiment_id: UUID,
        approval: Annotated[str | None, Header(alias=APPROVAL_HEADER)] = None,
    ) -> UUID:
        route = f"{request.method} {request.url.path}"
        if approval is None:
            logger.warning("approval missing on %s", route)
            raise MarketError(401, ErrorCode.UNAUTHORIZED, f"{APPROVAL_HEADER} header is required")
        try:
            approval_id = UUID(approval)
        except ValueError:
            approval_id = None
        if approval_id is None or not grants.allows(approval_id, experiment_id):
            logger.warning(
                "approval denied: approval_id=%s experiment_id=%s on %s",
                approval,
                experiment_id,
                route,
            )
            raise MarketError(
                403, ErrorCode.EXPERIMENT_NOT_APPROVED, "This approval does not allow the call"
            )
        logger.info(
            "approval allowed: approval_id=%s experiment_id=%s on %s",
            approval_id,
            experiment_id,
            route,
        )
        return approval_id

    return require_approval


def research_scope(
    request: Request,
    experiment_id: UUID,
    account: Annotated[str | None, Header(alias=ACCOUNT_HEADER)] = None,
) -> PageScope:
    """For routes without an account in the path (news, filings): the account comes from the
    X-Bazaar-Account header and must belong to the path's experiment. Mount the route behind
    approval_check too; this dependency does not check the approval.
    """
    if account is None:
        raise MarketError(401, ErrorCode.UNAUTHORIZED, f"{ACCOUNT_HEADER} header is required")
    try:
        account_id = UUID(account)
    except ValueError:
        raise MarketError(
            403, ErrorCode.FORBIDDEN, "The account is not in this experiment"
        ) from None
    ledger: Ledger = request.app.state.ledger
    return ledger.page_scope(experiment_id, account_id)


def runner_token_check(expected: str | None) -> Callable[..., None]:
    """Control routes only. With no token configured, every call is refused."""
    if not expected:
        logger.warning("BAZAAR_RUNNER_TOKEN is not set; control routes refuse every call")

    def require_runner_token(
        request: Request,
        token: Annotated[str | None, Header(alias=RUNNER_TOKEN_HEADER)] = None,
    ) -> None:
        if (
            not expected
            or token is None
            or not hmac.compare_digest(token.encode(), expected.encode())
        ):
            logger.warning("runner token refused on %s %s", request.method, request.url.path)
            raise MarketError(
                401, ErrorCode.UNAUTHORIZED, f"A valid {RUNNER_TOKEN_HEADER} header is required"
            )

    return require_runner_token


def build_routers(
    ledger: Ledger, grants: GrantChecker, runner_token: str | None
) -> tuple[APIRouter, APIRouter]:
    """The agent routes (approval only) and the runner's control routes (token and approval)."""
    require_approval = approval_check(grants)
    router = APIRouter(dependencies=[Depends(require_approval)])
    control = APIRouter(
        dependencies=[Depends(runner_token_check(runner_token)), Depends(require_approval)]
    )

    @control.put("/experiments/{experiment_id}/cutoff")
    def set_cutoff(experiment_id: UUID, body: CutoffRequest) -> CutoffResponse:
        experiment = ledger.set_cutoff(
            experiment_id, body.cutoff, body.data_version, body.execution_rule_version
        )
        return CutoffResponse(experiment_id=experiment_id, cutoff=experiment.cutoff_at)

    @control.post("/experiments/{experiment_id}/accounts", status_code=201)
    def create_account(experiment_id: UUID, body: CreateAccountRequest) -> AccountSnapshot:
        return ledger.create_account(
            experiment_id,
            request_id=body.request_id,
            agent_id=body.agent_id,
            strategy_version_id=body.strategy_version_id,
            cash=Decimal(body.cash),
        )

    @router.get("/experiments/{experiment_id}/accounts/{account_id}")
    def get_account(experiment_id: UUID, account_id: UUID) -> AccountSnapshot:
        return ledger.account(experiment_id, account_id)

    @router.post("/experiments/{experiment_id}/accounts/{account_id}/orders")
    def submit_order(
        experiment_id: UUID, account_id: UUID, body: OrderRequest
    ) -> FilledOrder | RejectedOrder:
        return ledger.submit(experiment_id, account_id, body)

    @router.get("/experiments/{experiment_id}/accounts/{account_id}/orders")
    def order_history(
        experiment_id: UUID,
        account_id: UUID,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: str = "100",
        cursor: str | None = None,
    ) -> OrderHistoryPage:
        request = parse_history_request(start_at, end_at, limit, cursor)
        scope, results = ledger.order_history(experiment_id, account_id)
        return build_page(
            OrderHistoryPage,
            scope,
            request,
            route="orders",
            source=ORDER_HISTORY_SOURCE,
            items=[((r.account.simulated_at, str(r.order_id)), r) for r in results],
        )

    @control.post("/experiments/{experiment_id}/accounts/{account_id}/close")
    def close_account(experiment_id: UUID, account_id: UUID) -> AccountSnapshot:
        return ledger.close_account(experiment_id, account_id)

    @router.get("/experiments/{experiment_id}/accounts/{account_id}/portfolio")
    def get_portfolio(experiment_id: UUID, account_id: UUID) -> PortfolioSnapshot:
        return ledger.portfolio(experiment_id, account_id)

    return router, control


def install(app: FastAPI, ledger: Ledger, grants: GrantChecker, runner_token: str | None) -> None:
    for router in build_routers(ledger, grants, runner_token):
        app.include_router(router)

    @app.exception_handler(MarketError)
    async def market_error(request: Request, error: MarketError) -> JSONResponse:
        return _error(error.status_code, error.code, error.message)

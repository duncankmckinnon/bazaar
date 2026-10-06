"""HTTP routes for the clock, accounts and orders. Every route requires a live approval.

The approval id travels in the X-Bazaar-Approval header. A missing header is 401; an approval the
GrantChecker does not allow is 403 experiment_not_approved. Both are decided before the request
body is read, so a denied call writes nothing.
"""

import logging
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
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from bazaar_market.db import MarketError
from bazaar_market.ledger import Ledger

logger = logging.getLogger(__name__)

APPROVAL_HEADER = "X-Bazaar-Approval"


class GrantChecker(Protocol):
    def allows(self, approval_id: UUID) -> bool: ...


class DenyAllGrants:
    """The default until the approval service (#18) exists."""

    def allows(self, approval_id: UUID) -> bool:
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


def build_router(ledger: Ledger, grants: GrantChecker) -> APIRouter:
    def require_approval(
        request: Request,
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
        if approval_id is None or not grants.allows(approval_id):
            logger.warning("approval denied: approval_id=%s on %s", approval, route)
            raise MarketError(
                403, ErrorCode.EXPERIMENT_NOT_APPROVED, "This approval does not allow the call"
            )
        logger.info("approval allowed: approval_id=%s on %s", approval_id, route)
        return approval_id

    router = APIRouter(dependencies=[Depends(require_approval)])

    @router.put("/experiments/{experiment_id}/cutoff")
    def set_cutoff(experiment_id: UUID, body: CutoffRequest) -> CutoffResponse:
        experiment = ledger.set_cutoff(
            experiment_id, body.cutoff, body.data_version, body.execution_rule_version
        )
        return CutoffResponse(experiment_id=experiment_id, cutoff=experiment.cutoff_at)

    @router.post("/experiments/{experiment_id}/accounts", status_code=201)
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

    @router.post("/experiments/{experiment_id}/accounts/{account_id}/close")
    def close_account(experiment_id: UUID, account_id: UUID) -> AccountSnapshot:
        return ledger.close_account(experiment_id, account_id)

    @router.get("/experiments/{experiment_id}/accounts/{account_id}/portfolio")
    def get_portfolio(experiment_id: UUID, account_id: UUID) -> PortfolioSnapshot:
        return ledger.portfolio(experiment_id, account_id)

    return router


def install(app: FastAPI, ledger: Ledger, grants: GrantChecker) -> None:
    app.include_router(build_router(ledger, grants))

    @app.exception_handler(MarketError)
    async def market_error(request: Request, error: MarketError) -> JSONResponse:
        return _error(error.status_code, error.code, error.message)

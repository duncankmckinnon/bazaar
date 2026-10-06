"""The market as the runner sees it: agent-facing routes plus the trusted control calls."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from bazaar_protocol import (
    AccountSnapshot,
    ErrorCode,
    ErrorDetail,
    ExperimentContext,
    FilledOrder,
    Holding,
    OrderRequest,
    PortfolioSnapshot,
    PriceObservation,
    RejectedOrder,
)


class MarketError(Exception):
    """A market call that failed outright (an ApiError), as opposed to a RejectedOrder."""

    def __init__(self, detail: ErrorDetail) -> None:
        super().__init__(f"{detail.code}: {detail.message}")
        self.detail = detail


class ApprovalDenied(MarketError):
    """The market refused the run's approval (403 experiment_not_approved); nothing was written."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorDetail(code=ErrorCode.EXPERIMENT_NOT_APPROVED, message=message))


class FutureData(MarketError):
    """A read past the experiment's trusted cutoff (403 forbidden); never answered with data."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorDetail(code=ErrorCode.FORBIDDEN, message=message))


class MissingPrice(MarketError):
    """No close for the symbol is available at the cutoff; never filled with a default."""

    def __init__(self, symbol: str, cutoff: datetime) -> None:
        super().__init__(
            ErrorDetail(
                code=ErrorCode.DATA_UNAVAILABLE,
                message=f"no {symbol} close available at {cutoff.isoformat()}",
            )
        )
        self.symbol = symbol
        self.cutoff = cutoff


class MarketPort(Protocol):
    """Matches docs/market-agent-api.md plus bazaar-market's MarketControl (runner PLAN.md Part D).

    The grant argument is added once the GrantVerifier lands.
    """

    async def set_cutoff(
        self,
        experiment_id: UUID,
        cutoff: datetime,
        data_version: str,
        execution_rule_version: str,
    ) -> datetime:
        """Create the experiment clock on first call; later calls are monotonic."""
        ...

    async def create_account(
        self,
        experiment_id: UUID,
        agent_id: UUID,
        strategy_version_id: UUID,
        cash: Decimal,
        holdings: Sequence[Holding] = (),
        *,
        request_id: UUID,
    ) -> AccountSnapshot: ...

    async def account(self, ctx: ExperimentContext) -> AccountSnapshot: ...

    async def submit(
        self, ctx: ExperimentContext, order: OrderRequest
    ) -> FilledOrder | RejectedOrder: ...

    async def portfolio(self, ctx: ExperimentContext) -> PortfolioSnapshot:
        """Only at the current cutoff; the market keeps no mark history."""
        ...

    async def price_at(self, symbol: str, cutoff: datetime) -> PriceObservation:
        """The latest daily close whose available_at <= cutoff.

        Raises MissingPrice if there is none, and FutureData if cutoff is later than the
        experiment's current trusted cutoff.
        """
        ...

    async def close_account(self, experiment_id: UUID, account_id: UUID) -> AccountSnapshot: ...

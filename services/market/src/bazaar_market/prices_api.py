"""Agent price reads, cut off at the experiment's trusted clock.

The app provides two things on `app.state`: `clock`, with `experiment(experiment_id)`, and
`market_data_for`, which returns the `SqliteMarketData` for an experiment's data version.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from bazaar_protocol import (
    ApiError,
    ErrorCode,
    ErrorDetail,
    PriceHistory,
    PriceHistoryRequest,
)
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .clock import SqliteClock
from .prices import FutureDataError, MissingData, SqliteMarketData

router = APIRouter()


def _error(status: int, code: ErrorCode, message: str) -> JSONResponse:
    error = ApiError(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=error.model_dump(mode="json"))


@router.get(
    "/experiments/{experiment_id}/prices/{symbol}",
    response_model=PriceHistory,
    responses={code: {"model": ApiError} for code in (403, 404, 422)},
)
def price_history(
    request: Request,
    experiment_id: UUID,
    symbol: str,
    start_at: str,
    end_at: str,
    limit: str = "100",
) -> PriceHistory | JSONResponse:
    try:
        query = PriceHistoryRequest(symbol=symbol, start_at=start_at, end_at=end_at, limit=limit)
    except ValidationError as exc:
        return _error(422, ErrorCode.INVALID_REQUEST, str(exc.errors()[0]["msg"]))

    clock: SqliteClock = request.app.state.clock
    market_data_for: Callable[[UUID], SqliteMarketData] = request.app.state.market_data_for
    try:
        experiment = clock.experiment(experiment_id)
        market_data = market_data_for(experiment_id)
    except LookupError:
        return _error(404, ErrorCode.NOT_FOUND, "Unknown experiment")
    try:
        observations = market_data.price_history(
            query.symbol, query.start_at, query.end_at, experiment.cutoff_at, query.limit
        )
        source = market_data.price_source
    except FutureDataError:
        return _error(403, ErrorCode.FORBIDDEN, "end_at is after the experiment's current time")
    except MissingData:
        return _error(404, ErrorCode.NOT_FOUND, f"No prices for {query.symbol}")
    return PriceHistory(
        experiment_id=experiment_id,
        symbol=query.symbol,
        cutoff_at=experiment.cutoff_at,
        source=source,
        # The experiment's version (a bundle id, or a plain bars version), which the agent's
        # client checks against its context; the bars component it resolves to is the source.
        data_version=experiment.data_version,
        observations=observations,
    )

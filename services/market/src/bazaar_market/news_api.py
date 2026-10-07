"""Archived news for an agent, cut off at the experiment's trusted clock.

The app mounts `router` behind the approval check and provides `app.state.component`, which
maps an experiment and a kind to the archive version its data bundle names, and
`app.state.market_db_path`. The account comes from the X-Bazaar-Account header through
`ledger_api.research_scope`, never from the query.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from bazaar_protocol import ApiError, ErrorCode, Symbol
from bazaar_protocol.research import ArchivedNews, NewsPage
from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError

from .archive import MissingCoverage
from .bundles import NoComponent
from .clock import UnknownExperiment
from .db import MarketError
from .history import PageScope, build_page, parse_history_request
from .ledger_api import research_scope
from .news import NEWS_SOURCE, SqliteNewsArchive

router = APIRouter()
symbol_adapter = TypeAdapter(Symbol)


def _missing(message: str) -> MarketError:
    return MarketError(404, ErrorCode.DATA_UNAVAILABLE, message)


@router.get(
    "/experiments/{experiment_id}/news/{symbol}",
    response_model=NewsPage,
    responses={code: {"model": ApiError} for code in (401, 403, 404, 422)},
)
def news(
    request: Request,
    experiment_id: UUID,
    symbol: str,
    scope: Annotated[PageScope, Depends(research_scope)],
    start_at: str,
    end_at: str,
    limit: str = "100",
    cursor: str | None = None,
) -> NewsPage:
    """Articles published in [start_at, end_at] whose revision on file is available by the cutoff.

    An article revised after the cutoff is left out, because only its latest revision is on
    file. A window the archive was not fetched for is 404, never an empty page. The fetch
    selects by revision time, so an article revised after the fetch window ended is absent.
    """
    query = parse_history_request(start_at, end_at, limit, cursor)
    try:
        symbol = symbol_adapter.validate_python(symbol)
    except ValidationError:
        raise MarketError(422, ErrorCode.INVALID_REQUEST, "Invalid symbol") from None
    if query.end_at > scope.cutoff_at:
        raise MarketError(403, ErrorCode.FORBIDDEN, "end_at is after the experiment's current time")
    try:
        version = request.app.state.component(experiment_id, "news")
    except (UnknownExperiment, NoComponent):
        raise _missing("This experiment's data has no news archive") from None
    archive = SqliteNewsArchive(request.app.state.market_db_path, version)
    try:
        records = archive.visible(symbol, query.start_at, query.end_at, scope.cutoff_at)
    except MissingCoverage:
        raise _missing(f"News for {symbol} does not cover the requested window") from None
    source = f"{NEWS_SOURCE}/{version}"
    items = [
        (
            (r.published_at, r.record_id),
            ArchivedNews(
                source=source,
                data_version=scope.data_version,
                record_id=r.record_id,
                revision=r.revision,
                published_at=r.published_at,
                revised_at=r.available_at,
                available_at=r.available_at,
                symbol=symbol,
                headline=r.headline,
                text=r.text,
            ),
        )
        for r in records
    ]
    return build_page(
        NewsPage, scope, query, route="news", source=source, items=items, symbol=symbol
    )

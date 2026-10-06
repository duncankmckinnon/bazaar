"""Company filings for an agent, cut off at the experiment's trusted clock.

Mounted like `news_api`, behind the approval check, with the account from X-Bazaar-Account.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from bazaar_protocol import ApiError, ErrorCode, Symbol
from bazaar_protocol.research import CompanyFiling, FilingPage
from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError

from .archive import MissingCoverage
from .db import MarketError
from .filings import FILINGS_SOURCE, SqliteFilingArchive
from .history import PageScope, build_page, parse_history_request
from .ledger_api import research_scope

router = APIRouter()
symbol_adapter = TypeAdapter(Symbol)


def _missing(message: str) -> MarketError:
    return MarketError(404, ErrorCode.DATA_UNAVAILABLE, message)


@router.get(
    "/experiments/{experiment_id}/filings/{symbol}",
    response_model=FilingPage,
    responses={code: {"model": ApiError} for code in (401, 403, 404, 422)},
)
def filings(
    request: Request,
    experiment_id: UUID,
    symbol: str,
    scope: Annotated[PageScope, Depends(research_scope)],
    start_at: str,
    end_at: str,
    limit: str = "100",
    cursor: str | None = None,
) -> FilingPage:
    """10-K and 10-Q filings, and their amendments, accepted in [start_at, end_at] by the cutoff.

    Each carries the fiscal period its own XBRL facts report. 8-K filings have no fiscal period
    and are not served, and neither is a 10-K or 10-Q whose XBRL has no matching period. Text is
    the primary document as plain text, cut at 200,000 characters with an explicit marker. The
    archive covers acceptances from 2024-07-01, when document downloads start; a window outside
    that is 404, never an empty page.
    """
    query = parse_history_request(start_at, end_at, limit, cursor)
    try:
        symbol = symbol_adapter.validate_python(symbol)
    except ValidationError:
        raise MarketError(422, ErrorCode.INVALID_REQUEST, "Invalid symbol") from None
    if query.end_at > scope.cutoff_at:
        raise MarketError(403, ErrorCode.FORBIDDEN, "end_at is after the experiment's current time")
    try:
        version = request.app.state.component(experiment_id, "filings")
    except LookupError:
        raise _missing("This experiment's data has no filings archive") from None
    archive = SqliteFilingArchive(request.app.state.market_db_path, version)
    try:
        records = archive.visible(symbol, query.start_at, query.end_at, scope.cutoff_at)
    except MissingCoverage:
        raise _missing(f"Filings for {symbol} do not cover the requested window") from None
    source = f"{FILINGS_SOURCE}/{version}"
    items = [
        (
            (r.accepted_at, r.accession),
            CompanyFiling(
                source=source,
                data_version=scope.data_version,
                record_id=r.accession,
                revision=r.accession,
                published_at=r.accepted_at,
                revised_at=r.accepted_at,
                available_at=r.accepted_at,
                symbol=symbol,
                fiscal_period_start=r.period_start,
                fiscal_period_end=r.period_end,
                form=r.form,
                text=r.text,
            ),
        )
        for r in records
    ]
    return build_page(
        FilingPage, scope, query, route="filings", source=source, items=items, symbol=symbol
    )

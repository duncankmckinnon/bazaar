"""Company filings for an agent, cut off at the experiment's trusted clock.

Mounted like `news_api`, behind the approval check, with the account from X-Bazaar-Account.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from bazaar_protocol import ApiError, ErrorCode, Symbol, WireModel
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
MAX_CYCLE_SYMBOLS = 50


class FiscalCycleStart(WireModel):
    """The same shape as the agent client's FiscalCycle: when a company's current cycle began."""

    symbol: Symbol
    start: date


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


@router.get(
    "/experiments/{experiment_id}/fiscal-cycles",
    response_model=list[FiscalCycleStart],
    responses={code: {"model": ApiError} for code in (403, 404, 422)},
)
def fiscal_cycles(request: Request, experiment_id: UUID, symbols: str) -> list[FiscalCycleStart]:
    """Each company's current fiscal cycle at the experiment's cutoff, for the runner.

    The response is a bare JSON list of {"symbol": "AAPL", "start": "YYYY-MM-DD"}, exactly the
    fields of the agent client's FiscalCycle, so the runner can build FiscalCycle(**item).

    `start` is the day after the latest fiscal period end among the company's served 10-K and
    10-Q filings accepted by the cutoff. It uses only filings visible then and no fiscal-year
    arithmetic, so it can lag the real cycle but never runs ahead of it. The latest period end,
    not the latest filing's, so a late amendment for an older period never moves it back. A
    company with no such filing, or an experiment whose filings are not imported, is omitted,
    which the agent's client treats as unsupported. A malformed symbol is 422.
    """
    wanted = list(dict.fromkeys(s.strip() for s in symbols.split(",") if s.strip()))
    if not 1 <= len(wanted) <= MAX_CYCLE_SYMBOLS:
        raise MarketError(
            422, ErrorCode.INVALID_REQUEST, f"Give 1 to {MAX_CYCLE_SYMBOLS} comma-separated symbols"
        )
    try:
        wanted = [symbol_adapter.validate_python(s) for s in wanted]
    except ValidationError:
        raise MarketError(422, ErrorCode.INVALID_REQUEST, "Invalid symbol") from None
    try:
        experiment = request.app.state.clock.experiment(experiment_id)
    except LookupError:
        raise MarketError(404, ErrorCode.NOT_FOUND, "Unknown experiment") from None
    ends: dict[str, date] = {}
    try:
        version = request.app.state.component(experiment_id, "filings")
        archive = SqliteFilingArchive(request.app.state.market_db_path, version)
        ends = archive.latest_period_ends(wanted, experiment.cutoff_at)
    except (LookupError, MissingCoverage):
        pass  # no filings for this experiment: every symbol is omitted
    return [
        FiscalCycleStart(symbol=s, start=ends[s] + timedelta(days=1)) for s in wanted if s in ends
    ]

"""GET /experiments/{eid}/market/{screen,quotes,search,sectors}: market-wide reads for an agent.

Mounted behind the approval check. There is no client as_of: screen, quotes and sectors are
computed at the experiment's cutoff (see market_view), and say which session they used. Unknown
or repeated query parameters are refused with 422.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from bazaar_protocol import ApiError, ErrorCode
from fastapi import APIRouter, Request
from pydantic import BaseModel

from bazaar_market import db, market_view
from bazaar_market.bundles import SYMBOL
from bazaar_market.clock import Experiment, UnknownExperiment
from bazaar_market.db import MarketError

router = APIRouter(prefix="/experiments/{experiment_id}/market")
RESPONSES = {code: {"model": ApiError} for code in (401, 403, 404, 409, 422)}
SORTS = ("pct_change_1d", "pct_change_5d", "volume")
MAX_QUOTES = 50


class Envelope(BaseModel):
    cutoff_at: datetime
    as_of_session: date
    data_version: str


class ScreenRow(BaseModel):
    symbol: str
    name: str
    sector: str
    close: str
    pct_change_1d: str | None
    pct_change_5d: str | None
    volume: int
    observed_at: datetime


class Screen(Envelope):
    sort: Literal["pct_change_1d", "pct_change_5d", "volume"]
    direction: Literal["desc", "asc"]
    sector: str | None
    excluded: int
    rows: list[ScreenRow]


class QuoteRow(BaseModel):
    symbol: str
    name: str
    sector: str
    close: str
    observed_at: datetime
    available_at: datetime
    pct_change_1d: str | None
    pct_change_5d: str | None
    volume: int


class MissingQuote(BaseModel):
    symbol: str
    reason: Literal["unknown_symbol", "no_bar_at_cutoff"]


class Quotes(Envelope):
    quotes: list[QuoteRow]
    missing: list[MissingQuote]


class SearchResult(BaseModel):
    symbol: str
    name: str
    sector: str


class Search(BaseModel):
    data_version: str
    query: str
    results: list[SearchResult]


class SectorRow(BaseModel):
    sector: str
    symbols: int
    with_bars: int
    avg_pct_change_1d: str | None
    avg_pct_change_5d: str | None
    advancers: int
    decliners: int
    unchanged: int


class Benchmark(BaseModel):
    symbol: str
    close: str | None
    pct_change_1d: str | None
    pct_change_5d: str | None
    observed_at: datetime | None


class Sectors(Envelope):
    sectors: list[SectorRow]
    benchmark: Benchmark | None


def _invalid(message: str) -> MarketError:
    return MarketError(422, ErrorCode.INVALID_REQUEST, message)


def _params(request: Request, allowed: set[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    unknown = sorted(set(names) - allowed)
    if unknown:
        raise _invalid(f"Unknown query parameter: {unknown[0]}")
    if len(set(names)) != len(names):
        raise _invalid("A query parameter is repeated")
    return dict(items)


def _limit(raw: str | None, default: int, most: int) -> int:
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= most:
        raise _invalid(f"limit must be an integer from 1 to {most}")
    return int(raw)


def _one_of(raw: str | None, default: str, choices: tuple[str, ...], name: str) -> str:
    value = default if raw is None else raw
    if value not in choices:
        raise _invalid(f"{name} must be one of {', '.join(choices)}")
    return value


def _experiment(request: Request, experiment_id: UUID) -> Experiment:
    try:
        return request.app.state.clock.experiment(experiment_id)
    except UnknownExperiment:
        raise MarketError(404, ErrorCode.NOT_FOUND, "Unknown experiment") from None


def _no_universe() -> MarketError:
    return MarketError(404, ErrorCode.DATA_UNAVAILABLE, "This experiment's data has no universe")


def _view(request: Request, experiment_id: UUID) -> market_view.View:
    experiment = _experiment(request, experiment_id)
    with db.read_connection(request.app.state.market_db_path) as connection:
        try:
            return market_view.view(connection, experiment.data_version, experiment.cutoff_at)
        except market_view.NoUniverse:
            raise _no_universe() from None
        except market_view.NotRunning:
            raise MarketError(
                409, ErrorCode.EXPERIMENT_NOT_RUNNING, "No session has closed by the cutoff"
            ) from None


def _envelope(snapshot: market_view.View) -> dict[str, object]:
    return {
        "cutoff_at": snapshot.cutoff_at,
        "as_of_session": snapshot.as_of,
        "data_version": snapshot.data_version,
    }


def _changes(quote: market_view.Quote) -> dict[str, str | None]:
    return {
        "pct_change_1d": market_view.percent(quote.change_1d),
        "pct_change_5d": market_view.percent(quote.change_5d),
    }


@router.get("/screen", response_model=Screen, responses=RESPONSES)
def screen(request: Request, experiment_id: UUID) -> Screen:
    params = _params(request, {"sort", "direction", "sector", "limit"})
    sort = _one_of(params.get("sort"), "pct_change_1d", SORTS, "sort")
    direction = _one_of(params.get("direction"), "desc", ("desc", "asc"), "direction")
    limit = _limit(params.get("limit"), 20, 50)
    sector = params.get("sector")
    snapshot = _view(request, experiment_id)
    if sector is not None and sector not in {q.member.sector for q in snapshot.constituents()}:
        raise _invalid("Unknown sector")
    rows, excluded = market_view.screen(snapshot, sort, direction == "desc", sector, limit)
    return Screen(
        **_envelope(snapshot),
        sort=sort,
        direction=direction,
        sector=sector,
        excluded=excluded,
        rows=[
            ScreenRow(
                symbol=q.member.symbol,
                name=q.member.name,
                sector=q.member.sector,
                close=q.close,
                volume=q.volume,
                observed_at=q.observed_at,
                **_changes(q),
            )
            for q in rows
        ],
    )


def _symbols(raw: str | None) -> list[str]:
    if raw is None:
        raise _invalid("symbols is required")
    symbols: list[str] = []
    for item in raw.split(","):
        symbol = item.strip().upper()
        if not SYMBOL.match(symbol):
            raise _invalid("Malformed symbol in symbols")
        if symbol not in symbols:
            symbols.append(symbol)
    if len(symbols) > MAX_QUOTES:
        raise _invalid(f"At most {MAX_QUOTES} symbols")
    return symbols


@router.get("/quotes", response_model=Quotes, responses=RESPONSES)
def quotes(request: Request, experiment_id: UUID) -> Quotes:
    symbols = _symbols(_params(request, {"symbols"}).get("symbols"))
    snapshot = _view(request, experiment_id)
    found, missing = [], []
    for symbol in symbols:
        quote = snapshot.quotes.get(symbol)
        if quote is None:
            missing.append(MissingQuote(symbol=symbol, reason="unknown_symbol"))
        elif quote.close is None:
            missing.append(MissingQuote(symbol=symbol, reason="no_bar_at_cutoff"))
        else:
            found.append(
                QuoteRow(
                    symbol=symbol,
                    name=quote.member.name,
                    sector=quote.member.sector,
                    close=quote.close,
                    observed_at=quote.observed_at,
                    available_at=quote.available_at,
                    volume=quote.volume,
                    **_changes(quote),
                )
            )
    return Quotes(**_envelope(snapshot), quotes=found, missing=missing)


@router.get("/search", response_model=Search, responses=RESPONSES)
def search(request: Request, experiment_id: UUID) -> Search:
    params = _params(request, {"q", "limit"})
    query = params.get("q")
    if query is None or not 1 <= len(query) <= 64 or not query.strip():
        raise _invalid("q must be 1 to 64 characters")
    limit = _limit(params.get("limit"), 10, 25)
    experiment = _experiment(request, experiment_id)
    with db.read_connection(request.app.state.market_db_path) as connection:
        try:
            universe = market_view.members(connection, experiment.data_version)
        except market_view.NoUniverse:
            raise _no_universe() from None
    return Search(
        data_version=experiment.data_version,
        query=query,
        results=[
            SearchResult(symbol=m.symbol, name=m.name, sector=m.sector)
            for m in market_view.search(universe, query, limit)
        ],
    )


@router.get("/sectors", response_model=Sectors, responses=RESPONSES)
def sectors(request: Request, experiment_id: UUID) -> Sectors:
    _params(request, set())
    snapshot = _view(request, experiment_id)
    benchmark = snapshot.quotes.get(snapshot.benchmark) if snapshot.benchmark else None
    return Sectors(
        **_envelope(snapshot),
        sectors=[
            SectorRow(
                sector=s.sector,
                symbols=s.symbols,
                with_bars=s.with_bars,
                avg_pct_change_1d=market_view.percent(s.avg_change_1d),
                avg_pct_change_5d=market_view.percent(s.avg_change_5d),
                advancers=s.advancers,
                decliners=s.decliners,
                unchanged=s.unchanged,
            )
            for s in market_view.sectors(snapshot)
        ],
        benchmark=None
        if benchmark is None
        else Benchmark(
            symbol=benchmark.member.symbol,
            close=benchmark.close,
            observed_at=benchmark.observed_at,
            **_changes(benchmark),
        ),
    )

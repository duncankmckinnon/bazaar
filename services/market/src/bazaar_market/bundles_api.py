"""GET /bundles/{bundle_id}: a bundle's versions, window, universe, benchmark and sessions.

Sessions are judged over the universe and the benchmark.

Reference data, so no credentials are needed (the market listens on 127.0.0.1 only).
"""

from datetime import date

from bazaar_protocol import ErrorCode
from fastapi import APIRouter, Request
from pydantic import BaseModel

from bazaar_market import bundles, db
from bazaar_market.db import MarketError

router = APIRouter()


class IncompleteSession(BaseModel):
    date: date
    missing: list[str]


class UniverseMember(BaseModel):
    symbol: str
    name: str
    sector: str


class BundleInfo(BaseModel):
    bundle_id: str
    bars: str
    news: str
    filings: str
    start_date: date
    end_date: date
    universe_name: str | None
    universe: list[UniverseMember]  # the benchmark is not in it
    benchmark: str | None
    expected_sessions: int
    sessions: list[date]
    incomplete_sessions: list[IncompleteSession]


@router.get("/bundles/{bundle_id}", response_model=BundleInfo)
def bundle(request: Request, bundle_id: str) -> BundleInfo:
    with db.read_connection(request.app.state.market_db_path) as connection:
        window = bundles.window(connection, bundle_id)
        if window is None:
            raise MarketError(404, ErrorCode.NOT_FOUND, "Unknown bundle")
        parts = {kind: bundles.component(connection, bundle_id, kind) for kind in ("bars", "news", "filings")}  # fmt: skip
        members = bundles.universe(connection, bundle_id)
        found = bundles.sessions(connection, parts["bars"], window, [m.symbol for m in members])
    return BundleInfo(
        bundle_id=bundle_id,
        **parts,
        start_date=window.start,
        end_date=window.end,
        universe_name=window.universe_name,
        universe=[
            UniverseMember(symbol=m.symbol, name=m.name, sector=m.sector)
            for m in members
            if not m.benchmark
        ],
        benchmark=window.benchmark,
        expected_sessions=len(found.expected),
        sessions=found.complete,
        incomplete_sessions=[
            IncompleteSession(date=d, missing=list(m)) for d, m in found.incomplete
        ],
    )

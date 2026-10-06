"""Fiscal cycles: each company's current cycle at a cutoff, from filings visible then."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from bazaar_agent.research import FiscalCycle, ResearchContext, ResearchTools
from bazaar_market.filings import SqliteFilingArchive
from bazaar_market.sources.filings_import import import_filings_snapshot
from bazaar_market.sources.snapshot import Snapshot
from bazaar_protocol import ExperimentContext
from bazaar_protocol.research import ResearchRequest

from .test_filings import CIK, WINDOW, columns
from .test_news_api import Market

FILINGS = [
    # accession, form, report date, accepted, period start
    ("k-1", "10-K", "2024-12-31", "2025-02-03T21:00:00.000Z", "2024-01-01"),
    ("q-1", "10-Q", "2025-03-31", "2025-05-01T20:00:00.000Z", "2025-01-01"),
    ("q-2", "10-Q", "2025-06-30", "2025-08-01T20:00:00.000Z", "2025-04-01"),
    # A late amendment of the 2024 annual report, accepted after q-2.
    ("k-1a", "10-K/A", "2024-12-31", "2025-09-02T20:00:00.000Z", "2024-01-01"),
]
Q2_ACCEPTED = datetime(2025, 8, 1, 20, tzinfo=UTC)


def snapshot(tmp_path):
    snap = Snapshot(tmp_path / "raw", source="edgar", version="v1")
    rows = [(a, form, end, accepted[:10], accepted) for a, form, end, accepted, _ in FILINGS]
    submissions = {"cik": f"{CIK:010d}", "filings": {"recent": columns(rows)}}
    points = [
        {"accn": a, "start": start, "end": end, "val": 1, "form": form, "filed": accepted[:10],
         "fy": 2025, "fp": "FY" if form.startswith("10-K") else "Q2"}
        for a, form, end, accepted, start in FILINGS
    ]  # fmt: skip
    facts = {"cik": CIK, "facts": {"us-gaap": {"Revenues": {"units": {"USD": points}}}}}
    snap.write(f"submissions/CIK{CIK:010d}.json", json.dumps(submissions).encode(), url="u", rows=4)
    snap.write(f"companyfacts/CIK{CIK:010d}.json", json.dumps(facts).encode(), url="u", rows=4)
    for accession, *_ in FILINGS:
        snap.write(f"documents/{CIK}/{accession}/doc.htm", b"<p>report</p>", url="u", rows=1)
    return snap.dir


def import_into(db, tmp_path):
    with closing(sqlite3.connect(db)) as connection:
        import_filings_snapshot(connection, snapshot(tmp_path), {"ACME": CIK}, WINDOW)


@pytest.fixture
def archive(tmp_path):
    import_into(tmp_path / "m.db", tmp_path)
    return SqliteFilingArchive(tmp_path / "m.db")


def test_a_cutoff_between_two_10q_acceptances_starts_after_the_earlier_period(archive):
    cutoff = datetime(2025, 6, 15, tzinfo=UTC)

    assert archive.latest_period_ends(["ACME"], cutoff) == {"ACME": date(2025, 3, 31)}


def test_a_filing_counts_from_the_instant_it_was_accepted(archive):
    assert archive.latest_period_ends(["ACME"], Q2_ACCEPTED) == {"ACME": date(2025, 6, 30)}
    before = Q2_ACCEPTED - timedelta(microseconds=1)
    assert archive.latest_period_ends(["ACME"], before) == {"ACME": date(2025, 3, 31)}


def test_a_late_amendment_for_an_older_period_does_not_move_the_cycle_back(archive):
    cutoff = datetime(2025, 9, 3, tzinfo=UTC)

    assert archive.latest_period_ends(["ACME"], cutoff) == {"ACME": date(2025, 6, 30)}


def test_a_company_with_nothing_accepted_yet_is_absent(archive):
    assert archive.latest_period_ends(["ACME", "OTHR"], datetime(2025, 1, 1, tzinfo=UTC)) == {}


@pytest.fixture
def market(tmp_path):
    market = Market(tmp_path)
    import_into(market.app.state.market_db_path, tmp_path)
    yield market
    market.close()


def cycles(market: Market, symbols: str) -> httpx.Response:
    return market.http.get(
        f"/experiments/{market.experiment_id}/fiscal-cycles",
        params={"symbols": symbols},
        headers={"X-Bazaar-Approval": str(market.approval)},
    )


def test_the_route_returns_a_bare_list_shaped_like_the_clients_fiscal_cycle(market):
    response = cycles(market, "ACME,OTHR")

    assert response.status_code == 200
    assert response.json() == [{"symbol": "ACME", "start": "2025-07-01"}]
    assert FiscalCycle(**response.json()[0]).start == date(2025, 7, 1)


def test_the_route_needs_only_an_approval_for_the_experiment(market):
    response = market.http.get(
        f"/experiments/{market.experiment_id}/fiscal-cycles",
        params={"symbols": "ACME"},
        headers={"X-Bazaar-Approval": str(market.approvals.grant(market.agent_id))},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "symbols", ["", "acme", "ACME,bad symbol", ",".join(f"S{i}" for i in range(51))]
)
def test_malformed_or_too_many_symbols_are_rejected(market, symbols):
    assert cycles(market, symbols).status_code == 422


def test_a_bundle_without_filings_omits_every_symbol(tmp_path):
    market = Market(tmp_path)
    try:
        response = cycles(market, "ACME,AAPL")
        assert (response.status_code, response.json()) == (200, [])
    finally:
        market.close()


async def test_the_clients_filings_accept_the_cycles_from_this_route(market):
    found = [FiscalCycle(**item) for item in cycles(market, "ACME").json()]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=market.app),
        base_url="http://market",
        headers=market.headers(),
    )
    context = ExperimentContext(
        experiment_id=market.experiment_id,
        agent_id=market.agent_id,
        account_id=market.account_id,
        strategy_version_id=market.strategy_version_id,
        approval_id=market.approval,
        simulated_at=market.now,
        event_sequence=0,
        data_version=market.data_version,
        execution_rule_version="exec-v1",
    )
    research = ResearchTools(client, ResearchContext(experiment=context, cycles=tuple(found)))
    async with client:
        result = await research.filings(
            ResearchRequest(
                symbol="ACME", start_at=datetime(2024, 7, 1, tzinfo=UTC), end_at=market.now
            )
        )

    assert result.error is None
    assert [f.record_id for f in result.data.items] == ["k-1", "q-1", "q-2", "k-1a"]
    assert all(f.fiscal_period_end < found[0].start for f in result.data.items)


def test_an_unexpected_lookup_bug_is_not_turned_into_an_empty_list(market):
    def broken(experiment_id, kind):
        raise KeyError("a bug, not a missing archive")

    market.app.state.component = broken

    with pytest.raises(KeyError):
        cycles(market, "ACME")

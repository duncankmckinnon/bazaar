"""Session closes are 16:00 America/New_York: 20:00Z under EDT, 21:00Z under EST.

These go through the real path, a frozen Alpaca snapshot imported into data_bars and a fill
against the imported closes, so a fixed UTC offset anywhere would fail here.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from bazaar_market.ledger import Ledger
from bazaar_market.prices import SqliteMarketData
from bazaar_market.sources.bars_import import import_bars_snapshot
from bazaar_protocol import FilledOrder, OrderRequest

from .sources.test_alpaca_bars import freeze, payload

# The last session before and the first after each 2025-2026 clock change, plus the v2 window.
SESSIONS = {
    date(2025, 10, 31): "2025-10-31T20:00:00.000000+00:00",  # EDT, Friday before fall back
    date(2025, 11, 3): "2025-11-03T21:00:00.000000+00:00",  # EST, Monday after
    date(2026, 3, 6): "2026-03-06T21:00:00.000000+00:00",  # EST, Friday before spring forward
    date(2026, 3, 9): "2026-03-09T20:00:00.000000+00:00",  # EDT, Monday after
    date(2026, 9, 28): "2026-09-28T20:00:00.000000+00:00",  # EDT, first v2 session
    date(2026, 10, 9): "2026-10-09T20:00:00.000000+00:00",  # EDT, last v2 session
}
DAYS = sorted(SESSIONS)


@pytest.fixture
def db(tmp_path):
    snap = freeze(tmp_path, {"KO": [payload("KO", DAYS)]}, start=DAYS[0], end=DAYS[-1])
    path = tmp_path / "market.db"
    with closing(sqlite3.connect(path)) as connection:
        import_bars_snapshot(connection, snap.dir, data_version="alpaca-bars-v2", required={})
    return path


def test_the_import_stamps_each_close_at_16_00_new_york(db):
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT observed_at FROM data_bars WHERE data_version = 'alpaca-bars-v2' "
            "AND symbol = 'KO' ORDER BY observed_at"
        ).fetchall()

    assert [r[0] for r in rows] == [SESSIONS[d] for d in DAYS]


@pytest.mark.parametrize("day", DAYS)
def test_the_session_close_matches_the_stored_stamp(db, day):
    session = SqliteMarketData(db, "alpaca-bars-v2").session(day)

    assert session.close_at == datetime.fromisoformat(SESSIONS[day])


def submit_at(db, cutoff):
    ledger = Ledger(db, lambda version: SqliteMarketData(db, version))
    ledger.initialize()
    eid = uuid4()
    ledger.set_cutoff(eid, cutoff, "alpaca-bars-v2", "exec-v1")
    account = ledger.create_account(
        eid, request_id=uuid4(), agent_id=uuid4(), strategy_version_id=uuid4(), cash=Decimal(1000)
    )
    buy = OrderRequest(client_order_id=uuid4(), symbol="KO", side="buy", quantity="1")
    return ledger.submit(eid, account.account_id, buy)


def test_an_order_on_an_edt_day_fills_at_that_days_20_00z_close(db):
    edt_close = datetime(2026, 10, 9, 20, tzinfo=UTC)

    result = submit_at(db, edt_close)

    assert isinstance(result, FilledOrder)
    assert result.executed_at == result.price_available_at == edt_close
    assert result.unit_price == Decimal("101.5")


def test_a_second_before_the_edt_close_the_day_has_not_closed(db):
    result = submit_at(db, datetime(2026, 10, 9, 19, 59, 59, tzinfo=UTC))

    # The fixture's previous session is 2026-09-28, so the fill uses that close.
    assert isinstance(result, FilledOrder)
    assert result.price_available_at == datetime(2026, 9, 28, 20, tzinfo=UTC)

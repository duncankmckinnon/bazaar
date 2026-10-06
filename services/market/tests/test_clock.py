from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from bazaar_market import db
from bazaar_market.clock import SqliteClock, UnknownExperiment

START = datetime(2025, 7, 1, 20, 0, tzinfo=UTC)


@pytest.fixture
def clock(tmp_path):
    db.initialize(tmp_path / "market.db")
    return SqliteClock(tmp_path / "market.db")


def test_unknown_experiment_raises(clock):
    with pytest.raises(UnknownExperiment):
        clock.cutoff(uuid4())


def test_first_cutoff_requires_versions(clock):
    with pytest.raises(db.MarketError) as error:
        clock.set_cutoff(uuid4(), START)
    assert error.value.status_code == 422


def test_cutoff_moves_forward_only(clock):
    eid = uuid4()
    clock.set_cutoff(eid, START, "fixture-v1", "exec-v1")
    assert clock.cutoff(eid) == START

    assert clock.set_cutoff(eid, START).cutoff_seq == 1
    later = clock.set_cutoff(eid, START + timedelta(days=1))
    assert (later.cutoff_at, later.cutoff_seq) == (START + timedelta(days=1), 2)

    with pytest.raises(db.MarketError) as error:
        clock.set_cutoff(eid, START)
    assert error.value.status_code == 409
    assert clock.cutoff(eid) == START + timedelta(days=1)


def test_versions_cannot_change(clock):
    eid = uuid4()
    clock.set_cutoff(eid, START, "fixture-v1", "exec-v1")
    clock.set_cutoff(eid, START, "fixture-v1", "exec-v1")
    with pytest.raises(db.MarketError) as error:
        clock.set_cutoff(eid, START + timedelta(hours=1), "fixture-v2")
    assert error.value.status_code == 409
    assert clock.cutoff(eid) == START


def test_non_utc_cutoff_is_refused(clock):
    eastern = timezone(timedelta(hours=-4))
    with pytest.raises(ValueError):
        clock.set_cutoff(uuid4(), datetime(2025, 7, 1, 16, 0, tzinfo=eastern), "v", "exec-v1")


def test_cutoff_survives_reopen(tmp_path):
    eid = uuid4()
    db.initialize(tmp_path / "market.db")
    SqliteClock(tmp_path / "market.db").set_cutoff(eid, START, "fixture-v1", "exec-v1")
    db.initialize(tmp_path / "market.db")
    assert SqliteClock(tmp_path / "market.db").cutoff(eid) == START


@pytest.mark.parametrize("versions", [("", "exec-v1"), ("fixture-v1", " ")])
def test_empty_versions_are_refused(clock, versions):
    with pytest.raises(db.MarketError) as error:
        clock.set_cutoff(uuid4(), START, *versions)
    assert error.value.status_code == 422

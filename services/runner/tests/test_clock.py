from datetime import UTC, date, datetime, timedelta, timezone
from itertools import pairwise
from uuid import UUID

import pytest
from bazaar_protocol import ExperimentContext
from bazaar_runner.clock import (
    ClockScript,
    EventKind,
    RunManifest,
    TradingSession,
    build_schedule,
    schedule_digest,
)
from pydantic import ValidationError

SESSION_LENGTH = timedelta(hours=6, minutes=30)


def session(day: date) -> TradingSession:
    # November 2025 is EST: 09:30-16:00 New York is 14:30-21:00 UTC.
    open_at = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
    return TradingSession(date=day, open_at=open_at, close_at=open_at + SESSION_LENGTH)


# Friday, then Monday to Wednesday; the weekend of 2025-11-08/09 has no session.
SESSIONS = tuple(session(date(2025, 11, d)) for d in (7, 10, 11, 12))


def script(**updates) -> ClockScript:
    values = {
        "sessions": SESSIONS,
        "decision_offsets": (timedelta(minutes=30), timedelta(hours=4)),
    }
    return ClockScript(**(values | updates))


MANIFEST = RunManifest(
    experiment_id=UUID("00000000-0000-0000-0000-000000000001"),
    agent_id=UUID("00000000-0000-0000-0000-000000000002"),
    account_id=UUID("00000000-0000-0000-0000-000000000003"),
    strategy_version_id=UUID("00000000-0000-0000-0000-000000000004"),
    approval_id=UUID("00000000-0000-0000-0000-000000000005"),
    data_version="fixture-v1",
    execution_rule_version="immediate-v1",
)


def test_same_script_gives_identical_events_and_contexts():
    first, second = build_schedule(script()), build_schedule(script())
    assert first == second
    assert [e.context(MANIFEST) for e in first] == [e.context(MANIFEST) for e in second]


def test_schedule_shape():
    events = build_schedule(script())
    # Two decisions and one close mark per session.
    assert len(events) == 3 * len(SESSIONS)
    assert [e.kind for e in events[:3]] == [EventKind.DECISION, EventKind.DECISION, EventKind.MARK]
    assert events[0].simulated_at == datetime(2025, 11, 7, 15, 0, tzinfo=UTC)
    assert events[2].simulated_at == datetime(2025, 11, 7, 21, 0, tzinfo=UTC)


def test_ordering_key_strictly_increases_from_zero():
    events = build_schedule(script())
    assert [e.event_sequence for e in events] == list(range(len(events)))
    keys = [(e.simulated_at, e.event_sequence) for e in events]
    assert all(a < b for a, b in pairwise(keys))
    times = [e.simulated_at for e in events]
    assert times == sorted(times)


def test_every_context_is_a_valid_utc_experiment_context():
    for event in build_schedule(script()):
        ctx = event.context(MANIFEST)
        assert isinstance(ctx, ExperimentContext)
        assert ExperimentContext.model_validate_json(ctx.model_dump_json()) == ctx
        assert ctx.simulated_at.utcoffset() == timedelta(0)
        assert ctx.simulated_at == event.simulated_at
        assert ctx.event_sequence == event.event_sequence
        assert ctx.approval_id == MANIFEST.approval_id


def test_decision_at_close_orders_before_mark():
    events = build_schedule(script(decision_offsets=(SESSION_LENGTH,)))
    close = SESSIONS[0].close_at
    at_close = [e for e in events if e.simulated_at == close]
    assert [e.kind for e in at_close] == [EventKind.DECISION, EventKind.MARK]
    assert at_close[0].event_sequence < at_close[1].event_sequence


def test_non_session_days_yield_no_events():
    days = {e.simulated_at.date() for e in build_schedule(script())}
    assert days == {s.date for s in SESSIONS}
    assert date(2025, 11, 8) not in days
    assert date(2025, 11, 9) not in days


def test_digest_is_stable_and_tracks_the_script():
    digest = schedule_digest(build_schedule(script()))
    assert digest == schedule_digest(build_schedule(script()))
    assert len(digest) == 64

    early_close = SESSIONS[-1].model_copy(
        update={"close_at": SESSIONS[-1].open_at + timedelta(hours=5)}
    )
    changed_session = script(sessions=(*SESSIONS[:-1], early_close))
    assert schedule_digest(build_schedule(changed_session)) != digest

    changed_rule = script(decision_offsets=(timedelta(minutes=30), timedelta(hours=5)))
    assert schedule_digest(build_schedule(changed_rule)) != digest


@pytest.mark.parametrize(
    "updates",
    [
        {"sessions": ()},
        {"sessions": (SESSIONS[1], SESSIONS[0])},
        {"sessions": (SESSIONS[0], SESSIONS[0])},
        {"decision_offsets": ()},
        {"decision_offsets": (timedelta(hours=1), timedelta(minutes=30))},
        {"decision_offsets": (timedelta(minutes=-1),)},
        {"decision_offsets": (SESSION_LENGTH + timedelta(seconds=1),)},
    ],
)
def test_invalid_scripts_are_rejected(updates):
    with pytest.raises(ValidationError):
        script(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"close_at": SESSIONS[0].open_at},
        {"date": date(2025, 11, 8)},
        {"open_at": datetime(2025, 11, 7, 9, 30, tzinfo=timezone(timedelta(hours=-5)))},
    ],
)
def test_invalid_sessions_are_rejected(updates):
    values = SESSIONS[0].model_dump() | updates
    with pytest.raises(ValidationError):
        TradingSession(**values)

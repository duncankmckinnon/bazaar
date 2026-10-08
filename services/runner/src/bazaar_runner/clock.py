"""Scripted simulated clock: trading sessions become an ordered, replayable event schedule."""

import datetime as dt
import hashlib
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Self
from uuid import UUID

from bazaar_protocol import ExperimentContext, Version, WireModel
from pydantic import AwareDatetime, Field, TypeAdapter, model_validator


def utc_z(value: dt.datetime) -> str:
    """UTC with a Z suffix: the market rejects offsets, and a "+" in a query decodes as a space."""
    if value.utcoffset() != dt.timedelta(0):
        raise ValueError("market times must be UTC")
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


class TradingSession(WireModel):
    """One scripted trading session; the calendar is explicit, not derived from an exchange."""

    date: dt.date
    open_at: AwareDatetime
    close_at: AwareDatetime

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.open_at >= self.close_at:
            raise ValueError("a session must open before it closes")
        if self.open_at.date() != self.date:
            raise ValueError("a session must open on its own (UTC) date")
        return self


class ClockScript(WireModel):
    """Sessions plus the decision rule; each session also gets one mark at its close."""

    sessions: Annotated[tuple[TradingSession, ...], Field(min_length=1)]
    # Offsets after each session's open. A decision at the close orders before that close's mark.
    decision_offsets: Annotated[tuple[dt.timedelta, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def ordered_and_in_session(self) -> Self:
        for prev, nxt in pairwise(self.sessions):
            if prev.close_at > nxt.open_at or prev.date >= nxt.date:
                raise ValueError("sessions must be strictly ordered and must not overlap")
        offsets = self.decision_offsets
        if offsets[0] < dt.timedelta(0) or any(a >= b for a, b in pairwise(offsets)):
            raise ValueError("decision offsets must be nonnegative and strictly increasing")
        shortest = min(s.close_at - s.open_at for s in self.sessions)
        if offsets[-1] > shortest:
            raise ValueError("every decision offset must fall within every session")
        return self


class RunManifest(WireModel):
    """Run-constant fields copied into every event's ExperimentContext."""

    experiment_id: UUID
    agent_id: UUID
    account_id: UUID
    strategy_version_id: UUID
    approval_id: UUID
    data_version: Version
    execution_rule_version: Version


class EventKind(StrEnum):
    # Declaration order is the tie-break at equal simulated_at: decisions before marks.
    DECISION = "decision"
    MARK = "mark"


_KIND_RANK = {kind: rank for rank, kind in enumerate(EventKind)}


class ScheduledEvent(WireModel):
    kind: EventKind
    simulated_at: AwareDatetime
    event_sequence: Annotated[int, Field(ge=0, strict=True)]

    def context(self, manifest: RunManifest) -> ExperimentContext:
        return ExperimentContext(
            **manifest.model_dump(),
            simulated_at=self.simulated_at,
            event_sequence=self.event_sequence,
        )


Schedule = tuple[ScheduledEvent, ...]
_schedule_adapter = TypeAdapter(Schedule)


def build_schedule(script: ClockScript) -> Schedule:
    """Order every decision and mark by (simulated_at, kind) and number them from 0."""
    points = [
        (session.open_at + offset, EventKind.DECISION)
        for session in script.sessions
        for offset in script.decision_offsets
    ]
    points += [(session.close_at, EventKind.MARK) for session in script.sessions]
    points.sort(key=lambda point: (point[0], _KIND_RANK[point[1]]))
    return tuple(
        ScheduledEvent(kind=kind, simulated_at=at, event_sequence=sequence)
        for sequence, (at, kind) in enumerate(points)
    )


def trading_day(
    sessions: tuple[TradingSession, ...], at: dt.datetime
) -> tuple[int, int, dt.date] | None:
    """(N, total, first session date) for the session that contains `at`; N counts from 1."""
    for index, session in enumerate(sessions):
        if session.open_at <= at <= session.close_at:
            return index + 1, len(sessions), sessions[0].date
    return None


def schedule_digest(schedule: Schedule) -> str:
    """SHA-256 of the canonical schedule, so two runs can prove they share decisions and marks."""
    return hashlib.sha256(_schedule_adapter.dump_json(schedule)).hexdigest()

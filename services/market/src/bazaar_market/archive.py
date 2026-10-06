"""Helpers shared by the research archives (news and filings)."""

from __future__ import annotations

from datetime import UTC, datetime


class MissingCoverage(LookupError):
    """The archive was not fetched for the whole requested window. Never an empty result."""


class FutureDataError(Exception):
    """A read asked for time after the trusted cutoff."""


def stored_time(value: datetime) -> str:
    """One stored format, so stored instants compare correctly as strings."""
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def truncate(text: str, limit: int) -> str:
    """`text` cut so that it plus an explicit marker fits in `limit` characters."""
    if len(text) <= limit:
        return text
    marker = f"\n[truncated at {limit} characters]"
    return text[: limit - len(marker)] + marker

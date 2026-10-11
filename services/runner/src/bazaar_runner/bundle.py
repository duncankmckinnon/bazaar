"""The run's data bundle as the market declares it: its id, its universe and its trading sessions.

Read once per launch from the market's GET /bundles/{bundle_id}. The market derives the sessions
from its bars (the dates on which every universe symbol has one); the runner places each on the
New York clock and checks the list against the exchange calendar. Anything short, gappy or out of
the declared window stops the launch before any account exists.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from bazaar_protocol import Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bazaar_runner.clock import ClockScript, TradingSession

# The one bundle every demo launch and submission runs on. Nothing else names a bundle.
DEMO_BUNDLE = "demo-bundle-v2"
# Operator override, shared with the web app; never taken from strategy text or a request.
BUNDLE_ENV = "BAZAAR_DATA_BUNDLE"
# A leading letter or digit: "." and ".." would resolve to other market paths.
BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}")
NEW_YORK = ZoneInfo("America/New_York")
# Decide at the open (seeing the previous close), mark at the close; zoneinfo picks EST or EDT.
OPEN_AT, CLOSE_AT = time(9, 30), time(16, 0)
# NYSE full-day closures, by year. A window in a year not listed here fails rather than guesses.
NYSE_HOLIDAYS = {
    2026: frozenset(
        {
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 2, 16),
            date(2026, 4, 3),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 3),
            date(2026, 9, 7),
            date(2026, 11, 26),
            date(2026, 12, 25),
        }
    ),
}


class BundleError(Exception):
    """The market's bundle cannot carry a run. The message names the bundle and why."""


class IncompleteSession(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    date: date
    missing: tuple[str, ...]


class BundleInfo(BaseModel):
    """The market's GET /bundles/{bundle_id} body. Fields the market adds later are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    bundle_id: Version
    bars: Version
    news: Version
    filings: Version
    start_date: date
    end_date: date
    symbols: tuple[str, ...] = Field(min_length=1)
    sessions: tuple[date, ...]
    # Weekdays in the window less the market's own holiday list.
    expected_sessions: int
    incomplete_sessions: tuple[IncompleteSession, ...]


@dataclass(frozen=True)
class Bundle:
    """The launch-time value every run, policy and agent prompt takes its universe and days from."""

    bundle_id: str
    symbols: tuple[str, ...]
    script: ClockScript

    @property
    def sessions(self) -> tuple[TradingSession, ...]:
        return self.script.sessions


def check_bundle_id(value: str) -> str:
    """A bundle id starts with a letter or digit, then letters, digits, dots and dashes;
    anything else is refused unechoed."""
    if not BUNDLE_ID.fullmatch(value):
        raise BundleError(
            "a bundle id starts with a letter or digit and holds only letters, digits, dots"
            " and dashes"
        )
    return value


def run_bundle_id(environ: Mapping[str, str] = os.environ) -> str:
    """$BAZAAR_DATA_BUNDLE if set and non-empty, else DEMO_BUNDLE."""
    value = environ.get(BUNDLE_ENV, "")
    if not value:
        return DEMO_BUNDLE
    try:
        return check_bundle_id(value)
    except BundleError:
        raise BundleError(
            f"${BUNDLE_ENV} is not a bundle id (a letter or digit, then letters, digits, dots"
            " and dashes)"
        ) from None


def session_on(day: date) -> TradingSession:
    """09:30 to 16:00 New York on `day`, in UTC: 14:30Z-21:00Z in EST, 13:30Z-20:00Z in EDT."""
    return TradingSession(
        date=day,
        open_at=datetime.combine(day, OPEN_AT, NEW_YORK).astimezone(UTC),
        close_at=datetime.combine(day, CLOSE_AT, NEW_YORK).astimezone(UTC),
    )


def exchange_days(start: date, end: date) -> tuple[date, ...]:
    """Weekdays from start to end inclusive, less the NYSE holidays."""
    unknown = sorted({start.year, end.year} - NYSE_HOLIDAYS.keys())
    if unknown:
        raise BundleError(f"no NYSE holiday list for {', '.join(map(str, unknown))}")
    days = (start + timedelta(days=n) for n in range((end - start).days + 1))
    return tuple(d for d in days if d.weekday() < 5 and d not in NYSE_HOLIDAYS[d.year])


def bundle_from(info: BundleInfo) -> Bundle:
    """Check the market's sessions and build the run's clock from them, or raise BundleError."""
    problems = []
    if info.incomplete_sessions:
        gaps = "; ".join(f"{s.date} lacks {', '.join(s.missing)}" for s in info.incomplete_sessions)
        problems.append(f"bars are missing ({gaps})")
    outside = [d for d in info.sessions if not info.start_date <= d <= info.end_date]
    if outside:
        problems.append(
            f"sessions {', '.join(map(str, outside))} fall outside"
            f" {info.start_date}..{info.end_date}"
        )
    if len(info.sessions) != info.expected_sessions:
        problems.append(
            f"{len(info.sessions)} sessions where the market expects {info.expected_sessions}"
        )
    # The runner's own calendar too, so a disagreement between the two fails as well.
    expected = exchange_days(info.start_date, info.end_date)
    if len(expected) != info.expected_sessions:
        problems.append(
            f"the market expects {info.expected_sessions} sessions in"
            f" {info.start_date}..{info.end_date} and the NYSE calendar has {len(expected)}"
        )
    if info.sessions != expected:
        problems.append(
            f"sessions {', '.join(map(str, info.sessions))} are not the exchange days"
            f" {', '.join(map(str, expected))}"
        )
    if problems:
        raise BundleError(f"bundle {info.bundle_id} cannot run: " + "; ".join(problems))
    script = ClockScript(
        sessions=tuple(session_on(day) for day in info.sessions),
        decision_offsets=(timedelta(0),),
    )
    return Bundle(bundle_id=info.bundle_id, symbols=info.symbols, script=script)


async def read_bundle(client: httpx.AsyncClient, bundle_id: str) -> Bundle:
    """GET /bundles/{bundle_id} once, at launch. The route needs no credentials, so none are sent."""
    try:
        response = await client.get(f"/bundles/{quote(bundle_id, safe='')}")
    except httpx.TransportError as exc:
        raise BundleError(
            f"could not read bundle {bundle_id} from the market ({type(exc).__name__})"
        ) from None
    if response.status_code == 404:
        raise BundleError(f"the market has no bundle {bundle_id}")
    if response.is_error:
        raise BundleError(f"the market answered HTTP {response.status_code} for bundle {bundle_id}")
    try:
        info = BundleInfo.model_validate_json(response.content)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(map(str, e["loc"])) for e in exc.errors()}))
        raise BundleError(f"the market's bundle {bundle_id} is malformed ({fields})") from None
    if info.bundle_id != bundle_id:
        raise BundleError(f"asked the market for bundle {bundle_id} and got {info.bundle_id}")
    return bundle_from(info)

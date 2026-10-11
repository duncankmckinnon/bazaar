"""SEC EDGAR: filing history and reported numbers, with the time each filing was accepted.

`data.sec.gov` needs no key. `www.sec.gov`, which serves filing text, refuses callers whose
User-Agent does not name a contact address. The SEC asks for at most 10 requests a second.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .errors import SourceError
from .http import get
from .models import Fact, Filing
from .snapshot import Snapshot

DATA_URL = "https://data.sec.gov"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data"
EASTERN = ZoneInfo("America/New_York")
FILING_DAY_CUTOFF = clock(17, 30)
CUTOFF_FORMS = frozenset({"10-K", "10-Q", "8-K"})
# EDGAR dissemination starts at 06:00 Eastern on a business day. A label before that, or on a
# closed day, is treated as public from the next start.
DISSEMINATION_START = clock(6)

# Days EDGAR was closed beyond the federal holidays, each from the SEC's own notice. A day that is
# missing here only makes the label check stricter (it stops the import); never add one uncited.
AD_HOC_CLOSURES = {
    # https://www.sec.gov/newsroom/whats-new/edgar-be-closed-wednesday-december-5-2018
    date(2018, 12, 5): "national day of mourning, President George H. W. Bush",
    # https://www.sec.gov/oit/announcement/december-24-2018-federal-holiday-edgar-filing-purposes
    date(2018, 12, 24): "treated as a federal holiday",
    # https://www.sec.gov/newsroom/whats-new/edgar-closed-24-25-december-2024
    date(2024, 12, 24): "treated as a federal holiday",
    # https://www.sec.gov/newsroom/whats-new/edgar-closed-january-9-2025
    date(2025, 1, 9): "national day of mourning, President James Earl Carter, Jr.",
    # https://www.sec.gov/newsroom/whats-new/edgar-will-be-closed-december-24-2025-through-december-26-2025
    date(2025, 12, 24): "treated as a federal holiday",
    date(2025, 12, 26): "treated as a federal holiday",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """A fixed-date federal holiday on a Saturday is observed Friday, on a Sunday Monday."""
    return day + timedelta(days={5: -1, 6: 1}.get(day.weekday(), 0))


@cache
def federal_holidays(year: int) -> frozenset[date]:
    """The federal holidays EDGAR closes for (sec.gov EDGAR calendar), as observed."""
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    if year >= 2021:
        fixed.append(date(year, 6, 19))
    return frozenset(
        {
            *map(_observed, fixed),
            _nth_weekday(year, 1, 0, 3),  # Birthday of Martin Luther King, Jr.
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _last_weekday(year, 5, 0),  # Memorial Day
            _nth_weekday(year, 9, 0, 1),  # Labor Day
            _nth_weekday(year, 10, 0, 2),  # Columbus Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
        }
    )


def edgar_closed(day: date) -> bool:
    """Weekends, federal holidays and the cited ad hoc closures."""
    # A Saturday New Year's Day is observed on December 31 of the year before.
    holidays = federal_holidays(day.year) | federal_holidays(day.year + 1)
    return day.weekday() >= 5 or day in holidays or day in AD_HOC_CLOSURES


def next_dissemination_start(at: datetime) -> datetime:
    """The first 06:00 Eastern on an EDGAR business day at or after `at`."""
    labelled = at.astimezone(EASTERN)
    day = labelled.date()
    if edgar_closed(day) or labelled.time() >= DISSEMINATION_START:
        day += timedelta(days=1)
    while edgar_closed(day):
        day += timedelta(days=1)
    return datetime.combine(day, DISSEMINATION_START, tzinfo=EASTERN).astimezone(UTC)


def visible_at(filing: Filing) -> datetime:
    """When the filing is treated as public.

    Its label, when that falls inside EDGAR dissemination: on an EDGAR business day, at or after
    06:00 Eastern. Otherwise the next dissemination start, because a filing labelled overnight or
    on a closed day was not public until EDGAR opened. This only ever delays, and an after-hours
    label on a business day (17:30 to 22:00 Eastern) is kept.
    """
    labelled = filing.accepted_at.astimezone(EASTERN)
    if not edgar_closed(labelled.date()) and labelled.time() >= DISSEMINATION_START:
        return filing.accepted_at
    return next_dissemination_start(filing.accepted_at)


class ContactRequired(Exception):
    """Filing text comes from www.sec.gov, which needs a contact address in the User-Agent."""


def parse_submissions(
    payload: dict, *, cik: int | None = None, forms: tuple[str, ...] | None = None
) -> list[Filing]:
    """Parse a submissions response, or one of its older-filings files when `cik` is given."""
    columns = payload["filings"]["recent"] if "filings" in payload else payload
    company = int(payload["cik"]) if cik is None else cik
    filings = []
    for i, form in enumerate(columns["form"]):
        if forms is not None and form not in forms:
            continue
        report_date = columns["reportDate"][i]
        items = columns["items"][i]
        filings.append(
            Filing(
                cik=company,
                accession=columns["accessionNumber"][i],
                form=form,
                report_date=date.fromisoformat(report_date) if report_date else None,
                filing_date=date.fromisoformat(columns["filingDate"][i]),
                accepted_at=datetime.fromisoformat(columns["acceptanceDateTime"][i]),
                primary_document=columns["primaryDocument"][i],
                items=tuple(items.split(",")) if items else (),
            )
        )
    return filings


def check_acceptance_label(filing: Filing) -> None:
    """Raise when `accepted_at` is provably earlier than the real acceptance.

    EDGAR dates a 10-K, 10-Q or 8-K accepted on a business day before 17:30 Eastern with that
    day. If the labelled time falls on an EDGAR business day before 17:30 Eastern and the filing
    date is later, the filing was accepted later than labelled, and reading the label would show
    it early. A label on a day EDGAR was closed proves nothing: the filing date rolls to the next
    business day whatever the time. A label later than the real time is tolerated, since it only
    delays visibility.
    """
    if filing.form.removesuffix("/A") not in CUTOFF_FORMS:
        return
    labelled = filing.accepted_at.astimezone(EASTERN)
    on_business_day_before_cutoff = (
        not edgar_closed(labelled.date()) and labelled.time() < FILING_DAY_CUTOFF
    )
    if on_business_day_before_cutoff and filing.filing_date > labelled.date():
        raise SourceError(
            f"{filing.accession} ({filing.form}) is labelled as accepted at {filing.accepted_at}, "
            f"which is before the 17:30 Eastern cutoff on {labelled.date()}, but its filing date is "
            f"{filing.filing_date}. The label is earlier than the real acceptance."
        )


def parse_company_facts(payload: dict) -> list[Fact]:
    facts = []
    for taxonomy, concepts in payload["facts"].items():
        for concept, body in concepts.items():
            for unit, points in body["units"].items():
                for p in points:
                    facts.append(
                        Fact(
                            cik=int(payload["cik"]),
                            taxonomy=taxonomy,
                            concept=concept,
                            unit=unit,
                            value=p["val"],
                            period_start=date.fromisoformat(p["start"]) if "start" in p else None,
                            period_end=date.fromisoformat(p["end"]),
                            fiscal_year=p.get("fy"),
                            fiscal_period=p.get("fp"),
                            form=p["form"],
                            accession=p["accn"],
                            filed=date.fromisoformat(p["filed"]),
                        )
                    )
    return facts


class EdgarClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        user_agent: str,
        min_interval: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._user_agent = user_agent
        self._min_interval = min_interval
        self._sleep = sleep
        self._started = False

    def _get(self, url: str) -> bytes:
        if self._started:
            self._sleep(self._min_interval)
        self._started = True
        response = get(self._http, url, headers={"User-Agent": self._user_agent}, sleep=self._sleep)
        if response.status_code != 200:
            raise SourceError(f"EDGAR returned {response.status_code} for {url}")
        return response.content

    def fetch_company(self, cik: int, snap: Snapshot) -> list[Filing]:
        """Freeze one company's filing history and reported numbers. Returns every filing."""
        name = f"CIK{cik:010d}.json"
        url = f"{DATA_URL}/submissions/{name}"
        raw = self._get(url)
        submissions = json.loads(raw)
        filings = parse_submissions(submissions)
        snap.write(f"submissions/{name}", raw, url=url, rows=len(filings))

        for older in submissions["filings"].get("files", []):
            url = f"{DATA_URL}/submissions/{older['name']}"
            raw = self._get(url)
            more = parse_submissions(json.loads(raw), cik=cik)
            snap.write(f"submissions/{older['name']}", raw, url=url, rows=len(more))
            filings.extend(more)

        url = f"{DATA_URL}/api/xbrl/companyfacts/{name}"
        raw = self._get(url)
        facts = parse_company_facts(json.loads(raw))
        snap.write(f"companyfacts/{name}", raw, url=url, rows=len(facts))
        return filings

    def fetch_document(self, filing: Filing, snap: Snapshot) -> Path:
        if "@" not in self._user_agent:
            raise ContactRequired(
                "Set SEC_USER_AGENT to a name and contact address to download filing text."
            )
        folder = filing.accession.replace("-", "")
        url = f"{ARCHIVE_URL}/{filing.cik}/{folder}/{filing.primary_document}"
        raw = self._get(url)
        name = f"documents/{filing.cik}/{filing.accession}/{filing.primary_document}"
        return snap.write(name, raw, url=url, rows=1)

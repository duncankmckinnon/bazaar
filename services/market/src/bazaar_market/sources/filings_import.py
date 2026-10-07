"""Import 10-K and 10-Q filings from a frozen EDGAR snapshot into the market database.

A filing's fiscal period is never guessed. It is the XBRL duration, reported under the filing's
own accession, that ends on the filing's period end (`reportDate`) and whose length fits the
form. A 10-K also reports prior-year comparatives and quarters, and a 10-Q reports year-to-date
durations, so the first duration found is not the answer. A filing with no such duration, or
more than one, is left out and counted.

Only filings accepted inside the document window are served, because only their text was
fetched. A qualifying filing in that window whose primary document was not frozen stops the
import.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from ..archive import html_to_text, truncate
from ..filings import FILINGS_VERSION, TEXT_LIMIT, ExcludedFiling, FilingRecord, import_filings
from .errors import SourceError
from .models import Fact, Filing
from .read import load_document, load_facts, load_filings

# Inclusive day counts. 52/53-week years run 364 or 371 days, and 13/14-week quarters 91 or 98.
PERIOD_DAYS = {"10-K": (350, 380), "10-Q": (84, 98)}
FISCAL_PERIODS = {"10-K": {"FY"}, "10-Q": {"Q1", "Q2", "Q3"}}


@dataclass(frozen=True)
class Excluded:
    accession: str
    form: str
    reason: str


@dataclass(frozen=True)
class CompanyFilings:
    symbol: str
    cik: int
    served: int
    truncated: int
    excluded: tuple[Excluded, ...]
    outside_window: int


def find_period(filing: Filing, facts: list[Fact]) -> tuple[date, date] | str:
    """The one duration `facts` (this filing's own) report for its period, or why there is none."""
    form = filing.form.removesuffix("/A")
    if form not in PERIOD_DAYS:
        return f"{filing.form} has no fiscal period"
    if filing.report_date is None:
        return "EDGAR gives no report date"
    if not facts:
        return "no XBRL facts under this accession"
    reported = {f.fiscal_period for f in facts if f.fiscal_period}
    if reported and not reported <= FISCAL_PERIODS[form]:
        return f"XBRL fiscal period {sorted(reported)} does not fit a {form}"
    ending = {
        (f.period_start, f.period_end)
        for f in facts
        if f.period_start is not None and f.period_end == filing.report_date
    }
    if not ending:
        return f"no XBRL duration ends on the report date {filing.report_date}"
    shortest, longest = PERIOD_DAYS[form]
    fitting = {p for p in ending if shortest <= (p[1] - p[0]).days + 1 <= longest}
    if len(fitting) != 1:
        lengths = sorted((p[1] - p[0]).days + 1 for p in ending)
        return (
            f"{len(fitting)} durations ending {filing.report_date} fit a {form} "
            f"({shortest}-{longest} days); lengths found: {lengths}"
        )
    return fitting.pop()


def fiscal_period(filing: Filing, facts: list[Fact]) -> tuple[date, date] | None:
    """The one duration `facts` (this filing's own) report for the filing's period, or None."""
    period = find_period(filing, facts)
    return None if isinstance(period, str) else period


def document_window(documents_since: date, until: date) -> tuple[datetime, datetime]:
    """Acceptances the fetch downloaded text for: the UTC dates `documents_since` to `until`."""
    return (
        datetime.combine(documents_since, time.min, tzinfo=UTC),
        datetime.combine(until, time.max, tzinfo=UTC),
    )


def import_filings_snapshot(
    connection: sqlite3.Connection,
    snapshot_dir: Path,
    companies: dict[str, int],
    window: tuple[datetime, datetime],
    *,
    data_version: str = FILINGS_VERSION,
) -> list[CompanyFilings]:
    """Load each company's full history, keep the 10-K and 10-Q filings that qualify, store them.

    `companies` maps symbol to CIK, and `window` is the acceptance window documents were
    fetched for. A history that was not frozen completely, or a qualifying filing in the window
    without its document, raises, so a company is never served from part of its filings.
    """
    records: list[FilingRecord] = []
    gaps: list[ExcludedFiling] = []
    report = []
    for symbol, cik in companies.items():
        facts_by_accession: dict[str, list[Fact]] = defaultdict(list)
        for fact in load_facts(snapshot_dir, cik):
            facts_by_accession[fact.accession].append(fact)
        served = truncated = outside_window = 0
        excluded: list[Excluded] = []
        for filing in load_filings(snapshot_dir, cik):
            if filing.form.removesuffix("/A") not in PERIOD_DAYS:
                continue
            if not window[0] <= filing.accepted_at <= window[1]:
                outside_window += 1
                continue
            period = find_period(filing, facts_by_accession.get(filing.accession, []))
            if isinstance(period, str):
                excluded.append(Excluded(filing.accession, filing.form, period))
                gaps.append(
                    ExcludedFiling(
                        symbol, filing.accession, filing.form, filing.accepted_at, period
                    )
                )
                continue
            document = load_document(snapshot_dir, filing)
            if document is None:
                raise SourceError(
                    f"{symbol} {filing.form} {filing.accession} was accepted inside the document "
                    "window but its primary document was not fetched"
                )
            text = html_to_text(document.decode("utf-8", errors="replace"))
            truncated += len(text) > TEXT_LIMIT
            records.append(
                FilingRecord(
                    symbol=symbol,
                    accession=filing.accession,
                    form=filing.form,
                    accepted_at=filing.accepted_at,
                    period_start=period[0],
                    period_end=period[1],
                    text=truncate(text, TEXT_LIMIT),
                )
            )
            served += 1
        report.append(
            CompanyFilings(symbol, cik, served, truncated, tuple(excluded), outside_window)
        )
    import_filings(connection, records, companies, window, gaps, data_version=data_version)
    return report

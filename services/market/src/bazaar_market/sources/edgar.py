"""SEC EDGAR: filing history and reported numbers, with the time each filing was accepted.

`data.sec.gov` needs no key. `www.sec.gov`, which serves filing text, refuses callers whose
User-Agent does not name a contact address. The SEC asks for at most 10 requests a second.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import httpx

from .errors import SourceError
from .http import get
from .models import Fact, Filing
from .snapshot import Snapshot

DATA_URL = "https://data.sec.gov"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data"


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

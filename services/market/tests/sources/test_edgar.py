import json
from datetime import UTC, date, datetime

import httpx
import pytest
from bazaar_market.sources.edgar import (
    ContactRequired,
    EdgarClient,
    parse_company_facts,
    parse_submissions,
)
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.models import Filing
from bazaar_market.sources.snapshot import Snapshot, UnsafePath

RECENT = {
    "accessionNumber": ["0000719739-23-000030", "0000719739-23-000021", "0000719739-23-000010"],
    "form": ["10-K/A", "10-K", "8-K"],
    "reportDate": ["2022-12-31", "2022-12-31", ""],
    "filingDate": ["2023-03-01", "2023-02-24", "2023-01-19"],
    "acceptanceDateTime": [
        "2023-03-01T14:00:00.000Z",
        "2023-02-24T21:43:08.000Z",
        "2023-01-19T21:05:11.000Z",
    ],
    "primaryDocument": ["sivb-20221231a.htm", "sivb-20221231.htm", "sivb-20230119.htm"],
    "items": ["", "", "2.02,9.01"],
}
OLDER = {
    "accessionNumber": ["0000719739-14-000007"],
    "form": ["10-Q"],
    "reportDate": ["2014-03-31"],
    "filingDate": ["2014-05-09"],
    "acceptanceDateTime": ["2014-05-09T20:15:00.000Z"],
    "primaryDocument": ["sivb-2014q1.htm"],
    "items": [""],
}
SUBMISSIONS = {
    "cik": "0000719739",
    "name": "SVB FINANCIAL GROUP",
    "filings": {"recent": RECENT, "files": [{"name": "CIK0000719739-submissions-001.json"}]},
}
FACTS = {
    "cik": 719739,
    "facts": {
        "us-gaap": {
            "NetIncomeLoss": {
                "units": {
                    "USD": [
                        {
                            "start": "2022-01-01",
                            "end": "2022-12-31",
                            "val": 1672000000,
                            "accn": "0000719739-23-000021",
                            "fy": 2022,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-02-24",
                        },
                        {
                            "start": "2022-01-01",
                            "end": "2022-12-31",
                            "val": 1509000000,
                            "accn": "0000719739-23-000030",
                            "fy": 2022,
                            "fp": "FY",
                            "form": "10-K/A",
                            "filed": "2023-03-01",
                        },
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [
                        {
                            "end": "2022-12-31",
                            "val": 211793000000,
                            "accn": "0000719739-23-000021",
                            "fy": 2022,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-02-24",
                        }
                    ]
                }
            },
        }
    },
}


def test_parse_submissions_turns_columns_into_one_filing_per_row():
    filings = parse_submissions(SUBMISSIONS)

    assert filings[1] == Filing(
        cik=719739,
        accession="0000719739-23-000021",
        form="10-K",
        report_date=date(2022, 12, 31),
        filing_date=date(2023, 2, 24),
        accepted_at=datetime(2023, 2, 24, 21, 43, 8, tzinfo=UTC),
        primary_document="sivb-20221231.htm",
        items=(),
    )


def test_parse_submissions_keeps_an_amendment_as_a_separate_filing():
    forms = [(f.form, f.accession) for f in parse_submissions(SUBMISSIONS)]

    assert ("10-K", "0000719739-23-000021") in forms
    assert ("10-K/A", "0000719739-23-000030") in forms


def test_parse_submissions_splits_8k_items_and_allows_a_missing_report_date():
    eight_k = parse_submissions(SUBMISSIONS)[2]

    assert eight_k.items == ("2.02", "9.01")
    assert eight_k.report_date is None


def test_parse_submissions_reads_an_older_filings_file_given_the_company_number():
    filings = parse_submissions(OLDER, cik=719739)

    assert [(f.cik, f.form, f.filing_date) for f in filings] == [(719739, "10-Q", date(2014, 5, 9))]


def test_parse_submissions_can_keep_only_the_requested_forms():
    filings = parse_submissions(SUBMISSIONS, forms=("10-K", "10-K/A"))

    assert [f.form for f in filings] == ["10-K/A", "10-K"]


def test_parse_company_facts_keeps_both_the_original_and_the_restated_value():
    facts = [f for f in parse_company_facts(FACTS) if f.concept == "NetIncomeLoss"]

    assert [(f.value, f.accession, f.filed) for f in facts] == [
        (1672000000.0, "0000719739-23-000021", date(2023, 2, 24)),
        (1509000000.0, "0000719739-23-000030", date(2023, 3, 1)),
    ]


def test_parse_company_facts_handles_a_balance_with_no_period_start():
    assets = next(f for f in parse_company_facts(FACTS) if f.concept == "Assets")

    assert (assets.period_start, assets.period_end) == (None, date(2022, 12, 31))
    assert (assets.fiscal_year, assets.fiscal_period, assets.unit) == (2022, "FY", "USD")


def serve(routes, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=body if isinstance(body, bytes) else json.dumps(body))

    return httpx.Client(transport=httpx.MockTransport(handler))


ROUTES = {
    "https://data.sec.gov/submissions/CIK0000719739.json": SUBMISSIONS,
    "https://data.sec.gov/submissions/CIK0000719739-submissions-001.json": OLDER,
    "https://data.sec.gov/api/xbrl/companyfacts/CIK0000719739.json": FACTS,
    "https://www.sec.gov/Archives/edgar/data/719739/000071973923000021/sivb-20221231.htm": b"<html>10-K</html>",
}


def test_fetch_company_freezes_submissions_older_files_and_facts(tmp_path):
    seen, naps = [], []
    client = EdgarClient(serve(ROUTES, seen), user_agent="Bazaar test", sleep=naps.append)
    snap = Snapshot(tmp_path, source="edgar", version="v1")

    filings = client.fetch_company(719739, snap)

    assert sorted(p.name for p in (tmp_path / "edgar" / "v1").rglob("*.json")) == [
        "CIK0000719739-submissions-001.json",
        "CIK0000719739.json",
        "CIK0000719739.json",
        "manifest.json",
    ]
    assert [f.form for f in filings] == ["10-K/A", "10-K", "8-K", "10-Q"]


def test_every_request_identifies_the_caller(tmp_path):
    seen = []
    client = EdgarClient(serve(ROUTES, seen), user_agent="Bazaar test", sleep=lambda _: None)

    client.fetch_company(719739, Snapshot(tmp_path, source="edgar", version="v1"))

    assert [r.headers["user-agent"] for r in seen] == ["Bazaar test"] * 3


def test_requests_are_spaced_to_stay_under_the_sec_rate_limit(tmp_path):
    naps = []
    client = EdgarClient(
        serve(ROUTES, []), user_agent="Bazaar test", min_interval=0.2, sleep=naps.append
    )

    client.fetch_company(719739, Snapshot(tmp_path, source="edgar", version="v1"))

    assert naps == [0.2, 0.2]  # three requests, a pause before the second and third


def test_a_refused_request_stops_the_fetch_with_the_status(tmp_path):
    client = EdgarClient(serve({}, []), user_agent="Bazaar test", sleep=lambda _: None)

    with pytest.raises(SourceError, match="404"):
        client.fetch_company(719739, Snapshot(tmp_path, source="edgar", version="v1"))


def test_filing_text_is_not_requested_without_a_contact_address(tmp_path):
    seen = []
    client = EdgarClient(serve(ROUTES, seen), user_agent="Bazaar test", sleep=lambda _: None)
    filing = parse_submissions(SUBMISSIONS)[1]

    with pytest.raises(ContactRequired):
        client.fetch_document(filing, Snapshot(tmp_path, source="edgar", version="v1"))

    assert seen == []


def test_filing_text_is_saved_from_the_archive_path_for_that_filing(tmp_path):
    client = EdgarClient(
        serve(ROUTES, []), user_agent="Bazaar test ops@example.test", sleep=lambda _: None
    )
    filing = parse_submissions(SUBMISSIONS)[1]

    path = client.fetch_document(filing, Snapshot(tmp_path, source="edgar", version="v1"))

    assert path.read_bytes() == b"<html>10-K</html>"
    assert path.relative_to(tmp_path / "edgar" / "v1").as_posix() == (
        "documents/719739/0000719739-23-000021/sivb-20221231.htm"
    )


def test_fetch_company_records_each_file_with_its_own_url_and_row_count(tmp_path):
    client = EdgarClient(serve(ROUTES, []), user_agent="Bazaar test", sleep=lambda _: None)
    snap = Snapshot(tmp_path, source="edgar", version="v1")

    client.fetch_company(719739, snap)

    manifest = json.loads((tmp_path / "edgar" / "v1" / "manifest.json").read_text())
    assert [(f["file"], f["url"], f["rows"]) for f in manifest["files"]] == [
        (
            "submissions/CIK0000719739.json",
            "https://data.sec.gov/submissions/CIK0000719739.json",
            3,
        ),
        (
            "submissions/CIK0000719739-submissions-001.json",
            "https://data.sec.gov/submissions/CIK0000719739-submissions-001.json",
            1,
        ),
        (
            "companyfacts/CIK0000719739.json",
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000719739.json",
            3,
        ),
    ]


def test_an_older_file_name_that_points_outside_the_snapshot_is_refused(tmp_path):
    hostile = {
        "cik": "0000719739",
        "filings": {"recent": RECENT, "files": [{"name": "../../../escape.json"}]},
    }
    routes = {
        "https://data.sec.gov/submissions/CIK0000719739.json": hostile,
        "https://data.sec.gov/escape.json": OLDER,
    }
    client = EdgarClient(serve(routes, []), user_agent="Bazaar test", sleep=lambda _: None)

    with pytest.raises(UnsafePath):
        client.fetch_company(719739, Snapshot(tmp_path / "raw", source="edgar", version="v1"))

    assert not list(tmp_path.rglob("escape.json"))

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from bazaar_market.archive import FutureDataError, MissingCoverage, html_to_text
from bazaar_market.filings import SqliteFilingArchive
from bazaar_market.sources.cli import main
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.filings_import import (
    document_window,
    find_period,
    import_filings_snapshot,
)
from bazaar_market.sources.models import Fact, Filing
from bazaar_market.sources.snapshot import Snapshot

CIK = 42
WINDOW = document_window(date(2024, 7, 1), date(2026, 9, 30))


def filing(form, report_date, accession="a-1", accepted="2025-02-03T21:00:00+00:00"):
    return Filing(
        cik=CIK,
        accession=accession,
        form=form,
        report_date=report_date,
        filing_date=datetime.fromisoformat(accepted).date(),
        accepted_at=datetime.fromisoformat(accepted),
        primary_document="doc.htm",
    )


def fact(start, end, fp="FY", accession="a-1"):
    return Fact(
        cik=CIK,
        taxonomy="us-gaap",
        concept="Revenues",
        unit="USD",
        value=1.0,
        period_start=start,
        period_end=end,
        fiscal_year=2024,
        fiscal_period=fp,
        form="10-K",
        accession=accession,
        filed=date(2025, 2, 3),
    )


def test_a_10k_takes_its_own_year_not_the_prior_year_comparative_or_a_quarter():
    end = date(2024, 12, 31)
    facts = [
        fact(date(2023, 1, 1), date(2023, 12, 31)),  # prior-year comparative
        fact(date(2024, 10, 1), end),  # fourth quarter, also ends on the report date
        fact(date(2024, 1, 1), end),  # the fiscal year
        fact(None, end),  # an instant (balance sheet)
    ]

    assert find_period(filing("10-K", end), facts) == (date(2024, 1, 1), end)


def test_a_10q_takes_its_quarter_not_the_year_to_date():
    end = date(2025, 6, 30)
    facts = [fact(date(2025, 1, 1), end, fp="Q2"), fact(date(2025, 4, 1), end, fp="Q2")]

    assert find_period(filing("10-Q", end), facts) == (date(2025, 4, 1), end)


def test_a_53_week_year_and_a_14_week_quarter_fit():
    assert find_period(
        filing("10-K", date(2024, 9, 28)), [fact(date(2023, 9, 24), date(2024, 9, 28))]
    ) == (date(2023, 9, 24), date(2024, 9, 28))
    assert find_period(
        filing("10-Q", date(2024, 12, 28)),
        [fact(date(2024, 9, 22), date(2024, 12, 28), fp="Q1")],
    ) == (date(2024, 9, 22), date(2024, 12, 28))


@pytest.mark.parametrize(
    ("form", "facts", "reason"),
    [
        ("10-K", [], "no XBRL facts"),
        ("10-K", [fact(date(2023, 1, 1), date(2023, 12, 31))], "no XBRL duration ends on"),
        ("10-K", [fact(date(2024, 7, 1), date(2024, 12, 31))], "0 durations ending"),
        ("10-K", [fact(date(2024, 1, 1), date(2024, 12, 31), fp="Q4")], "does not fit a 10-K"),
        (
            "10-K",
            [
                fact(date(2024, 1, 1), date(2024, 12, 31)),
                fact(date(2023, 12, 25), date(2024, 12, 31)),
            ],
            "2 durations ending",
        ),
        ("8-K", [fact(date(2024, 1, 1), date(2024, 12, 31))], "has no fiscal period"),
    ],
)
def test_a_filing_without_exactly_one_matching_period_is_excluded_with_a_reason(
    form, facts, reason
):
    result = find_period(filing(form, date(2024, 12, 31)), facts)

    assert isinstance(result, str) and reason in result


def test_the_period_is_not_guessed_without_a_report_date():
    facts = [fact(date(2024, 1, 1), date(2024, 12, 31))]

    assert find_period(filing("10-K", None), facts) == "EDGAR gives no report date"


def test_filing_html_becomes_plain_text_without_inline_xbrl_headers():
    html = (
        "<html><head><title>t</title></head><body><ix:header><ix:hidden>dei:Secret</ix:hidden>"
        "</ix:header><div>Item 7.</div><p>Revenue <b>grew</b> 5%&nbsp;.</p><style>p{}</style>"
        "</body></html>"
    )

    assert html_to_text(html) == "Item 7.\nRevenue grew 5% ."


def columns(rows):
    keys = ("accessionNumber", "form", "reportDate", "filingDate", "acceptanceDateTime")
    out = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    out["primaryDocument"] = ["doc.htm"] * len(rows)
    out["items"] = [""] * len(rows)
    return out


def edgar_snapshot(tmp_path, *, documents=None, body="<p>Annual report.</p>"):
    """One company: a 10-K in the window, a 10-Q before it, and an 8-K."""
    snap = Snapshot(tmp_path / "raw", source="edgar", version="v1")
    rows = [
        ("k-1", "10-K", "2024-12-31", "2025-02-03", "2025-02-03T21:00:00.000Z"),
        ("q-0", "10-Q", "2024-03-31", "2024-05-01", "2024-05-01T20:00:00.000Z"),
        ("e-1", "8-K", "", "2025-03-03", "2025-03-03T13:00:00.000Z"),
    ]
    submissions = {"cik": f"{CIK:010d}", "filings": {"recent": columns(rows)}}
    point = {"val": 1, "form": "10-K", "filed": "2025-02-03", "fy": 2024, "fp": "FY"}
    facts = {
        "cik": CIK,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {**point, "accn": "k-1", "start": "2024-01-01", "end": "2024-12-31"},
                            {**point, "accn": "k-1", "start": "2023-01-01", "end": "2023-12-31"},
                            {**point, "accn": "q-0", "fp": "Q1", "start": "2024-01-01",
                             "end": "2024-03-31"},
                        ]
                    }
                }
            }
        },
    }  # fmt: skip
    snap.write(f"submissions/CIK{CIK:010d}.json", json.dumps(submissions).encode(), url="u", rows=3)
    snap.write(f"companyfacts/CIK{CIK:010d}.json", json.dumps(facts).encode(), url="u", rows=3)
    for accession in documents if documents is not None else ["k-1", "q-0", "e-1"]:
        snap.write(f"documents/{CIK}/{accession}/doc.htm", body.encode(), url="u", rows=1)
    return snap.dir


def load(tmp_path, **kwargs):
    db = tmp_path / "m.db"
    with closing(sqlite3.connect(db)) as connection:
        report = import_filings_snapshot(
            connection, edgar_snapshot(tmp_path, **kwargs), {"ACME": CIK}, WINDOW
        )
    return SqliteFilingArchive(db), report


CUTOFF = datetime(2026, 2, 13, 21, tzinfo=UTC)
START = datetime(2024, 7, 1, tzinfo=UTC)


def test_only_10k_and_10q_inside_the_document_window_are_served(tmp_path):
    filings, [report] = load(tmp_path)

    [served] = filings.visible("ACME", START, CUTOFF, cutoff=CUTOFF)
    assert (served.accession, served.form) == ("k-1", "10-K")
    assert (served.period_start, served.period_end) == (date(2024, 1, 1), date(2024, 12, 31))
    assert served.text == "Annual report."
    assert (report.served, report.outside_window, report.excluded, report.truncated) == (
        1,
        1,
        (),
        0,
    )


def test_a_filing_is_visible_from_its_acceptance(tmp_path):
    filings, _ = load(tmp_path)
    accepted = datetime(2025, 2, 3, 21, tzinfo=UTC)

    assert filings.visible("ACME", START, accepted, cutoff=accepted) != []
    just_before = datetime(2025, 2, 3, 20, 59, 59, 999999, tzinfo=UTC)
    assert filings.visible("ACME", START, just_before, cutoff=just_before) == []


def test_a_window_starting_before_the_documents_is_missing(tmp_path):
    filings, _ = load(tmp_path)

    with pytest.raises(MissingCoverage):
        filings.visible("ACME", datetime(2024, 6, 30, tzinfo=UTC), CUTOFF, cutoff=CUTOFF)


def test_an_end_after_the_cutoff_is_refused(tmp_path):
    filings, _ = load(tmp_path)

    with pytest.raises(FutureDataError):
        filings.visible("ACME", START, CUTOFF, cutoff=START)


def test_a_qualifying_filing_without_its_document_stops_the_import(tmp_path):
    with pytest.raises(SourceError, match="k-1"):
        load(tmp_path, documents=["q-0"])


def test_long_text_is_cut_with_an_explicit_marker(tmp_path):
    filings, [report] = load(tmp_path, body="<p>" + "x" * 250_000 + "</p>")

    [served] = filings.visible("ACME", START, CUTOFF, cutoff=CUTOFF)
    assert len(served.text) == 200_000
    assert served.text.endswith("\n[truncated at 200000 characters]")
    assert report.truncated == 1


def test_reimporting_stores_each_filing_once(tmp_path):
    load(tmp_path)
    load(tmp_path)

    with closing(sqlite3.connect(tmp_path / "m.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM data_filings").fetchone()[0] == 1


def test_cli_import_filings_without_a_snapshot_says_to_fetch_edgar(tmp_path, capsys):
    code = main(
        ["import-filings", "--snapshot", str(tmp_path / "edgar-x"), "--db", str(tmp_path / "m.db")]
    )

    assert code == 1
    assert "sources edgar --version edgar-x first." in capsys.readouterr().err

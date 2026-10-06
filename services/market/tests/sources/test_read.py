import json
from datetime import UTC, date, datetime

import pytest
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.read import load_facts, load_filings, load_news, news_coverage
from bazaar_market.sources.snapshot import Snapshot


def columns(accession, form, filed):
    return {
        "accessionNumber": [accession],
        "form": [form],
        "reportDate": [""],
        "filingDate": [filed],
        "acceptanceDateTime": [f"{filed}T21:00:00.000Z"],
        "primaryDocument": ["d.htm"],
        "items": [""],
    }


def edgar_snapshot(tmp_path):
    snap = Snapshot(tmp_path, source="edgar", version="v1")
    recent = {
        "cik": "0000000042",
        "filings": {
            "recent": columns("a-2", "10-K", "2023-02-24"),
            "files": [{"name": "CIK0000000042-submissions-001.json"}],
        },
    }
    older = columns("a-1", "10-Q", "2014-05-09")
    other = {"cik": "0000000420", "filings": {"recent": columns("b-1", "10-K", "2023-01-01")}}
    facts = {
        "cik": 42,
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": "2022-12-31",
                                "val": 5,
                                "accn": "a-2",
                                "fy": 2022,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2023-02-24",
                            }
                        ]
                    }
                }
            }
        },
    }
    snap.write("submissions/CIK0000000042.json", json.dumps(recent).encode(), url="u", rows=1)
    snap.write(
        "submissions/CIK0000000042-submissions-001.json",
        json.dumps(older).encode(),
        url="u",
        rows=1,
    )
    snap.write("submissions/CIK0000000420.json", json.dumps(other).encode(), url="u", rows=1)
    snap.write("companyfacts/CIK0000000042.json", json.dumps(facts).encode(), url="u", rows=1)
    return snap.dir


def test_load_filings_joins_recent_and_older_files_for_one_company(tmp_path):
    filings = load_filings(edgar_snapshot(tmp_path), 42)

    assert [(f.cik, f.accession, f.filing_date) for f in filings] == [
        (42, "a-1", date(2014, 5, 9)),
        (42, "a-2", date(2023, 2, 24)),
    ]


def test_load_filings_does_not_pick_up_a_company_whose_number_shares_a_prefix(tmp_path):
    accessions = [f.accession for f in load_filings(edgar_snapshot(tmp_path), 42)]

    assert "b-1" not in accessions


def test_load_filings_can_keep_only_the_requested_forms(tmp_path):
    filings = load_filings(edgar_snapshot(tmp_path), 42, forms=("10-K",))

    assert [f.accession for f in filings] == ["a-2"]


def test_load_filings_fails_for_a_company_missing_from_the_snapshot(tmp_path):
    with pytest.raises(SourceError, match="99"):
        load_filings(edgar_snapshot(tmp_path), 99)


def test_load_facts_reads_the_frozen_numbers_for_one_company(tmp_path):
    facts = load_facts(edgar_snapshot(tmp_path), 42)

    assert [(f.concept, f.value, f.accession) for f in facts] == [("Assets", 5.0, "a-2")]


def page(*ids, token=None):
    return {
        "next_page_token": token,
        "news": [
            {
                "id": i,
                "headline": "h",
                "content": "c",
                "symbols": ["AAPL"],
                "created_at": f"2024-03-0{i}T10:00:00Z",
                "updated_at": f"2024-03-0{i}T10:00:00Z",
            }
            for i in ids
        ],
    }


def test_load_news_reads_every_page_and_drops_articles_repeated_across_pages(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1, 2, token="t")).encode(), url="u", rows=2)
    snap.write("AAPL/page-0002.json", json.dumps(page(2, 3)).encode(), url="u", rows=2)

    assert [n.id for n in load_news(snap.dir, "AAPL")] == ["1", "2", "3"]


def test_load_news_fails_for_a_symbol_missing_from_the_snapshot(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1)).encode(), url="u", rows=1)

    with pytest.raises(SourceError, match="MSFT"):
        load_news(snap.dir, "MSFT")


def test_load_filings_fails_when_an_older_filings_file_was_never_frozen(tmp_path):
    snap_dir = edgar_snapshot(tmp_path)
    (snap_dir / "submissions" / "CIK0000000042-submissions-001.json").unlink()

    with pytest.raises(SourceError, match="submissions-001"):
        load_filings(snap_dir, 42)


def test_load_news_fails_when_the_fetch_stopped_before_the_last_page(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1, token="more")).encode(), url="u", rows=1)

    with pytest.raises(SourceError, match="incomplete"):
        load_news(snap.dir, "AAPL")


def test_load_news_fails_when_a_page_in_the_middle_is_missing(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1, token="t")).encode(), url="u", rows=1)
    snap.write("AAPL/page-0003.json", json.dumps(page(3)).encode(), url="u", rows=1)

    with pytest.raises(SourceError, match="page-0002"):
        load_news(snap.dir, "AAPL")


def test_load_news_returns_articles_in_publication_order_whatever_the_page_order(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(3, 1, token="t")).encode(), url="u", rows=2)
    snap.write("AAPL/page-0002.json", json.dumps(page(2)).encode(), url="u", rows=1)

    assert [n.id for n in load_news(snap.dir, "AAPL")] == ["1", "2", "3"]


def test_load_facts_fails_for_a_company_missing_from_the_snapshot(tmp_path):
    with pytest.raises(SourceError, match="99"):
        load_facts(edgar_snapshot(tmp_path), 99)


def test_load_news_refuses_a_page_the_manifest_does_not_list(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1, token="t")).encode(), url="u", rows=1)
    stray = snap.dir / "AAPL" / "page-0002.json"
    stray.write_text(json.dumps(page(2)))

    with pytest.raises(SourceError, match="does not list"):
        load_news(snap.dir, "AAPL")


def test_load_news_refuses_a_symbol_whose_only_page_is_unlisted(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1)).encode(), url="u", rows=1)
    (snap.dir / "MSFT").mkdir()
    (snap.dir / "MSFT" / "page-0001.json").write_text(json.dumps(page(2)))

    with pytest.raises(SourceError, match="does not list"):
        load_news(snap.dir, "MSFT")


def test_load_news_refuses_a_page_edited_after_it_was_frozen(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1)).encode(), url="u", rows=1)
    snap.path("AAPL/page-0001.json").write_text(json.dumps(page(2)))

    with pytest.raises(SourceError, match="SHA-256"):
        load_news(snap.dir, "AAPL")


def test_load_filings_refuses_a_history_file_edited_after_it_was_frozen(tmp_path):
    snap_dir = edgar_snapshot(tmp_path)
    older = snap_dir / "submissions" / "CIK0000000042-submissions-001.json"
    older.write_text(json.dumps(columns("a-9", "10-Q", "2014-05-09")))

    with pytest.raises(SourceError, match="SHA-256"):
        load_filings(snap_dir, 42)


def test_load_facts_refuses_numbers_edited_after_they_were_frozen(tmp_path):
    snap_dir = edgar_snapshot(tmp_path)
    (snap_dir / "companyfacts" / "CIK0000000042.json").write_text("{}")

    with pytest.raises(SourceError, match="SHA-256"):
        load_facts(snap_dir, 42)


def test_news_coverage_is_the_window_recorded_at_fetch_time(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.cover("AAPL", start="2025-07-01T00:00:00Z", end="2025-07-03T10:00:00Z")
    snap.write("AAPL/page-0001.json", json.dumps(page(1)).encode(), url="u", rows=1)

    assert news_coverage(snap.dir, "AAPL") == (
        datetime(2025, 7, 1, tzinfo=UTC),
        datetime(2025, 7, 3, 10, tzinfo=UTC),
    )


def test_news_coverage_fails_when_no_window_was_recorded(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(page(1)).encode(), url="u", rows=1)

    with pytest.raises(SourceError, match="AAPL"):
        news_coverage(snap.dir, "AAPL")

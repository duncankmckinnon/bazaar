import json
from datetime import date

import httpx
import pytest
from bazaar_market.sources.cli import main
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.fetch import fetch_edgar, fetch_news_range, fetch_universe
from bazaar_market.sources.universe import load_config

CONFIG = """
[period]
start = 2022-06-01
end = 2023-06-30

[sp500]
commit = "a2430f2af0c79ddf0748e91de11bdeb1616ab5a7"

[edgar]
forms = ["10-K", "8-K"]
documents_since = 2022-01-01

[[company]]
ticker = "META"
cik = 1326801
news_symbols = ["META", "FB"]
"""
SP500_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/"
    "a2430f2af0c79ddf0748e91de11bdeb1616ab5a7/sp500_ticker_start_end.csv"
)
SP500_CSV = "ticker,start_date,end_date\nFB,2013-12-23,2022-06-09\nMETA,2022-06-09,\n"
SUBMISSIONS = {
    "cik": "0001326801",
    "filings": {
        "files": [],
        "recent": {
            "accessionNumber": [
                "0001326801-23-000013",
                "0001326801-22-000082",
                "0001326801-21-000014",
            ],
            "form": ["10-K", "10-Q", "10-K"],
            "reportDate": ["2022-12-31", "2022-06-30", "2020-12-31"],
            "filingDate": ["2023-02-02", "2022-07-28", "2021-01-28"],
            "acceptanceDateTime": [
                "2023-02-02T21:10:00.000Z",
                "2022-07-28T20:10:00.000Z",
                "2021-01-28T21:10:00.000Z",
            ],
            "primaryDocument": ["meta-20221231.htm", "meta-20220630.htm", "fb-20201231.htm"],
            "items": ["", "", ""],
        },
    },
}
FACTS = {"cik": 1326801, "facts": {}}
NEWS = {
    "news": [
        {
            "id": 9,
            "headline": "h",
            "content": "c",
            "symbols": ["META"],
            "created_at": "2022-06-10T12:00:00Z",
            "updated_at": "2022-06-10T12:00:00Z",
        }
    ],
    "next_page_token": None,
}


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(CONFIG)
    return load_config(path)


def web(seen, sp500=SP500_CSV):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url == SP500_URL:
            return httpx.Response(200, text=sp500)
        if url == "https://data.sec.gov/submissions/CIK0001326801.json":
            return httpx.Response(200, content=json.dumps(SUBMISSIONS))
        if url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0001326801.json":
            return httpx.Response(200, content=json.dumps(FACTS))
        if request.url.host == "www.sec.gov":
            return httpx.Response(200, text="<html>filing</html>")
        if request.url.host == "data.alpaca.markets":
            return httpx.Response(200, content=json.dumps(NEWS))
        return httpx.Response(404, text="unexpected " + url)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_universe_freezes_the_pinned_membership_file(cfg, tmp_path):
    members = fetch_universe(cfg, tmp_path / "raw", web([]))

    saved = tmp_path / "raw" / "sp500" / "a2430f2" / "sp500_ticker_start_end.csv"
    assert saved.read_text() == SP500_CSV
    assert [(m.ticker, m.end) for m in members] == [("FB", date(2022, 6, 9)), ("META", None)]


def test_fetch_universe_fails_when_a_configured_company_has_no_membership_history(cfg, tmp_path):
    only_fb = "ticker,start_date,end_date\nFB,2013-12-23,2022-06-09\n"

    with pytest.raises(SourceError, match="META"):
        fetch_universe(cfg, tmp_path / "raw", web([], sp500=only_fb))


def test_fetch_edgar_without_a_contact_skips_filing_text(cfg, tmp_path):
    seen = []

    summary = fetch_edgar(
        cfg, tmp_path / "raw", web(seen), version="v1", user_agent="Bazaar", sleep=lambda _: None
    )

    assert [r.url.host for r in seen] == ["data.sec.gov", "data.sec.gov"]
    assert summary == {"META": {"filings": 3, "documents": 0}}


def test_fetch_edgar_with_a_contact_downloads_configured_forms_since_the_cutoff(cfg, tmp_path):
    seen = []

    summary = fetch_edgar(
        cfg,
        tmp_path / "raw",
        web(seen),
        version="v1",
        user_agent="Bazaar ops@example.test",
        sleep=lambda _: None,
    )

    # The 10-Q is not a configured form and the 2021 10-K predates documents_since.
    assert [r.url.path for r in seen if r.url.host == "www.sec.gov"] == [
        "/Archives/edgar/data/1326801/000132680123000013/meta-20221231.htm"
    ]
    assert summary == {"META": {"filings": 3, "documents": 1}}


def test_fetch_news_range_fetches_each_news_symbol_of_each_company(cfg, tmp_path):
    seen = []

    summary = fetch_news_range(
        cfg,
        tmp_path / "raw",
        web(seen),
        version="v1",
        start=date(2022, 6, 1),
        end=date(2022, 6, 30),
    )

    assert [r.url.params["symbols"] for r in seen] == ["META", "FB"]
    assert summary == {"META": 1, "FB": 1}


def write_config(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(CONFIG)
    return str(path)


def test_cli_universe_writes_the_snapshot_and_succeeds(tmp_path, capsys):
    code = main(
        ["universe", "--config", write_config(tmp_path), "--root", str(tmp_path / "raw")],
        env={},
        http=web([]),
    )

    assert code == 0
    assert (tmp_path / "raw" / "sp500" / "a2430f2" / "manifest.json").exists()


def test_cli_news_refuses_to_run_without_alpaca_keys(tmp_path):
    seen = []

    with pytest.raises(SystemExit, match="ALPACA_API_KEY"):
        main(
            ["news", "--config", write_config(tmp_path), "--root", str(tmp_path / "raw")],
            env={},
            http=web(seen),
        )

    assert seen == []


def test_cli_news_sends_the_alpaca_keys_and_covers_the_configured_period(tmp_path):
    seen = []

    main(
        ["news", "--config", write_config(tmp_path), "--root", str(tmp_path / "raw")],
        env={"ALPACA_API_KEY": "id", "ALPACA_SECRET_KEY": "secret"},
        http=web(seen),
    )

    first = seen[0]
    assert (first.headers["apca-api-key-id"], first.headers["apca-api-secret-key"]) == (
        "id",
        "secret",
    )
    assert (first.url.params["start"], first.url.params["end"]) == (
        "2022-06-01T00:00:00Z",
        "2023-06-30T23:59:59Z",
    )


def test_cli_capture_news_covers_the_trailing_days_up_to_today(tmp_path):
    seen = []

    main(
        [
            "capture-news",
            "--days",
            "3",
            "--config",
            write_config(tmp_path),
            "--root",
            str(tmp_path / "raw"),
        ],
        env={"ALPACA_API_KEY": "id", "ALPACA_SECRET_KEY": "secret"},
        http=web(seen),
        today=date(2026, 10, 6),
    )

    assert (seen[0].url.params["start"], seen[0].url.params["end"]) == (
        "2026-10-04T00:00:00Z",
        "2026-10-06T23:59:59Z",
    )


def test_cli_does_not_leak_alpaca_keys_to_the_sec(tmp_path):
    seen = []

    main(
        ["edgar", "--config", write_config(tmp_path), "--root", str(tmp_path / "raw")],
        env={"ALPACA_API_KEY": "id", "ALPACA_SECRET_KEY": "secret", "SEC_REQUEST_INTERVAL": "0"},
        http=web(seen),
    )

    assert all("apca-api-key-id" not in r.headers for r in seen)

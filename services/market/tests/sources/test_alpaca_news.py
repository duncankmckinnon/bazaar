import json
from datetime import UTC, date, datetime

import httpx
import pytest
from bazaar_market.sources.alpaca_news import fetch_news, parse_news
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.snapshot import Snapshot, SnapshotConflict


def article(id_, created, updated, content="body"):
    return {
        "id": id_,
        "headline": f"headline {id_}",
        "content": content,
        "summary": "",
        "symbols": ["AAPL", "MSFT"],
        "created_at": created,
        "updated_at": updated,
        "source": "",
        "author": "a",
        "url": "u",
        "images": [],
    }


PAGE_1 = {
    "news": [
        article(1, "2024-03-04T01:31:31Z", "2024-03-04T01:32:12Z"),
        article(2, "2024-03-04T09:00:00Z", "2024-03-04T09:00:00Z", content=""),
    ],
    "next_page_token": "abc",
}
PAGE_2 = {
    "news": [article(3, "2024-03-05T10:00:00Z", "2024-03-05T10:00:00Z")],
    "next_page_token": None,
}

FROZEN_URL = (
    "https://data.alpaca.markets/v1beta1/news?symbols=AAPL&start=2024-03-04T00%3A00%3A00Z"
    "&end=2024-03-05T23%3A59%3A59Z&include_content=true&limit=50&sort=asc"
)


def freeze_first_page(snap, page):
    """What an earlier run of the 2024-03-04 to 2024-03-05 window left behind."""
    snap.cover("AAPL", start="2024-03-04T00:00:00Z", end="2024-03-05T23:59:59Z")
    snap.write("AAPL/page-0001.json", json.dumps(page).encode(), url=FROZEN_URL, rows=1)


def serve(pages, seen, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, text="forbidden")
        return httpx.Response(200, content=json.dumps(pages[len(seen) - 1]))

    return httpx.Client(transport=httpx.MockTransport(handler))


def run(tmp_path, seen, **kwargs):
    return fetch_news(
        serve([PAGE_1, PAGE_2], seen, **kwargs),
        symbol="AAPL",
        start=date(2024, 3, 4),
        end=date(2024, 3, 5),
        snap=Snapshot(tmp_path, source="alpaca-news", version="v1"),
    )


def test_parse_news_reads_ids_times_and_symbols():
    item = parse_news(PAGE_1)[0]

    assert (item.source, item.id, item.symbols) == ("alpaca", "1", ("AAPL", "MSFT"))
    assert item.created_at == datetime(2024, 3, 4, 1, 31, 31, tzinfo=UTC)
    assert item.updated_at == datetime(2024, 3, 4, 1, 32, 12, tzinfo=UTC)


def test_parse_news_flags_an_article_with_no_body():
    first, second = parse_news(PAGE_1)

    assert (first.has_body, second.has_body) == (True, False)


def test_fetch_news_follows_the_page_token_until_it_runs_out(tmp_path):
    seen = []

    items = run(tmp_path, seen)

    assert [i.id for i in items] == ["1", "2", "3"]
    assert [r.url.params.get("page_token") for r in seen] == [None, "abc"]


def test_fetch_news_asks_for_bodies_over_the_whole_of_both_days(tmp_path):
    seen = []

    run(tmp_path, seen)

    params = seen[0].url.params
    assert params["symbols"] == "AAPL"
    assert params["include_content"] == "true"
    assert (params["start"], params["end"]) == ("2024-03-04T00:00:00Z", "2024-03-05T23:59:59Z")


def test_fetch_news_freezes_every_page_it_received(tmp_path):
    run(tmp_path, [])

    saved = sorted(p.name for p in (tmp_path / "alpaca-news" / "v1" / "AAPL").iterdir())
    assert saved == ["page-0001.json", "page-0002.json"]
    manifest = json.loads((tmp_path / "alpaca-news" / "v1" / "manifest.json").read_text())
    assert [f["rows"] for f in manifest["files"]] == [2, 1]


def test_a_refused_news_request_stops_the_fetch_with_the_status(tmp_path):
    with pytest.raises(SourceError, match="403"):
        run(tmp_path, [], status=403)


def test_news_requests_are_spaced_to_stay_under_the_alpaca_rate_limit(tmp_path):
    naps = []

    fetch_news(
        serve([PAGE_1, PAGE_2], []),
        symbol="AAPL",
        start=date(2024, 3, 4),
        end=date(2024, 3, 5),
        snap=Snapshot(tmp_path, source="alpaca-news", version="v1"),
        min_interval=0.35,
        sleep=naps.append,
    )

    assert naps == [0.35]  # two pages, one pause between them


def test_an_interrupted_fetch_resumes_after_the_pages_already_frozen(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    freeze_first_page(snap, PAGE_1)
    seen = []

    items = fetch_news(
        serve([PAGE_2], seen),
        symbol="AAPL",
        start=date(2024, 3, 4),
        end=date(2024, 3, 5),
        snap=snap,
        sleep=lambda _: None,
    )

    assert [r.url.params.get("page_token") for r in seen] == ["abc"]
    assert [i.id for i in items] == ["1", "2", "3"]


def test_a_completed_fetch_makes_no_requests_when_run_again(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    freeze_first_page(snap, PAGE_2)
    seen = []

    items = fetch_news(
        serve([], seen), symbol="AAPL", start=date(2024, 3, 4), end=date(2024, 3, 5), snap=snap
    )

    assert seen == []
    assert [i.id for i in items] == ["3"]


def test_pages_frozen_for_another_window_are_not_reused(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    freeze_first_page(snap, PAGE_2)
    seen = []

    with pytest.raises(SnapshotConflict, match="2024-03-04"):
        fetch_news(
            serve([PAGE_2], seen),
            symbol="AAPL",
            start=date(2025, 1, 1),
            end=date(2025, 1, 31),
            snap=snap,
        )

    assert seen == []


def test_a_page_token_that_repeats_stops_the_fetch(tmp_path):
    looping = {**PAGE_1, "next_page_token": "abc"}

    with pytest.raises(SourceError, match="page token"):
        fetch_news(
            serve([looping, looping, looping, looping], []),
            symbol="AAPL",
            start=date(2024, 3, 4),
            end=date(2024, 3, 5),
            snap=Snapshot(tmp_path, source="alpaca-news", version="v1"),
            sleep=lambda _: None,
        )


def test_the_fetched_window_is_recorded_in_the_manifest(tmp_path):
    run(tmp_path, [])

    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    assert snap.coverage("AAPL") == {"start": "2024-03-04T00:00:00Z", "end": "2024-03-05T23:59:59Z"}


def test_until_ends_the_window_at_the_fetch_time(tmp_path):
    seen = []

    fetch_news(
        serve([PAGE_2], seen),
        symbol="AAPL",
        start=date(2024, 3, 4),
        end=date(2024, 3, 5),
        until=datetime(2024, 3, 5, 10, 0, 0, 123456, tzinfo=UTC),
        snap=Snapshot(tmp_path, source="alpaca-news", version="v1"),
    )

    assert seen[0].url.params["end"] == "2024-03-05T10:00:00Z"
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    assert snap.coverage("AAPL")["end"] == "2024-03-05T10:00:00Z"


def test_pages_frozen_without_a_recorded_window_are_not_reused(tmp_path):
    snap = Snapshot(tmp_path, source="alpaca-news", version="v1")
    snap.write("AAPL/page-0001.json", json.dumps(PAGE_2).encode(), url=FROZEN_URL, rows=1)
    seen = []

    with pytest.raises(SnapshotConflict, match="no recorded window"):
        fetch_news(
            serve([PAGE_2], seen),
            symbol="AAPL",
            start=date(2024, 3, 4),
            end=date(2024, 3, 5),
            snap=snap,
        )

    assert seen == []

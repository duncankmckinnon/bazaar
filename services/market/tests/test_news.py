import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from bazaar_market.archive import FutureDataError, MissingCoverage, truncate
from bazaar_market.news import NewsConflict, SqliteNewsArchive
from bazaar_market.sources.cli import main
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.news_import import import_news_snapshot
from bazaar_market.sources.snapshot import Snapshot

START, END = "2026-01-01T00:00:00Z", "2026-02-13T23:59:59Z"
T = datetime(2026, 2, 2, 15, 0, tzinfo=UTC)


def article(id_, created, updated=None, headline="h", content="body"):
    return {
        "id": id_,
        "headline": headline,
        "content": content,
        "symbols": ["AAPL"],
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": (updated or created).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def freeze(tmp_path, pages, *, start=START, end=END):
    """A synthetic alpaca-news snapshot: {symbol: [articles]} on one page each."""
    snap = Snapshot(tmp_path / "raw", source="alpaca-news", version="v1")
    for symbol, articles in pages.items():
        snap.cover(symbol, start=start, end=end)
        body = json.dumps({"news": articles, "next_page_token": None}).encode()
        snap.write(f"{symbol}/page-0001.json", body, url="u", rows=len(articles))
    return snap.dir


def load(tmp_path, pages, **kwargs):
    db = tmp_path / "m.db"
    with closing(sqlite3.connect(db)) as connection:
        report = import_news_snapshot(connection, freeze(tmp_path, pages), **kwargs)
    return SqliteNewsArchive(db), report


def ids(records):
    return [r.record_id for r in records]


def test_an_article_is_visible_once_its_revision_on_file_is_available(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T, T + timedelta(hours=2))]})
    window = (T - timedelta(days=1), T + timedelta(hours=1))

    assert news.visible("AAPL", *window, cutoff=T + timedelta(hours=1)) == []
    record = news.visible("AAPL", window[0], T + timedelta(hours=2), cutoff=T + timedelta(hours=2))
    assert (record[0].published_at, record[0].available_at) == (T, T + timedelta(hours=2))
    assert record[0].revision == (T + timedelta(hours=2)).isoformat()


def test_an_article_revised_after_the_cutoff_is_left_out_not_served_early(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T, T + timedelta(days=3))]})

    assert news.visible("AAPL", T, T + timedelta(days=1), cutoff=T + timedelta(days=1)) == []


def test_a_revision_stamped_before_creation_is_available_at_creation(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T, T - timedelta(hours=1))]})

    [record] = news.visible("AAPL", T, T, cutoff=T)
    assert record.published_at <= record.available_at == T


def test_the_cutoff_instant_is_included_and_one_microsecond_before_is_not(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T)]})

    assert ids(news.visible("AAPL", T - timedelta(days=1), T, cutoff=T)) == ["1"]
    before = T - timedelta(microseconds=1)
    assert news.visible("AAPL", T - timedelta(days=1), before, cutoff=before) == []


def test_articles_are_selected_by_publication_and_ordered_by_time_then_id(tmp_path):
    news, _ = load(
        tmp_path,
        {"AAPL": [article(3, T), article(2, T), article(1, T - timedelta(days=5))]},
    )

    assert ids(news.visible("AAPL", T - timedelta(days=1), T, cutoff=T)) == ["2", "3"]


def test_an_end_after_the_cutoff_is_refused(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T)]})

    with pytest.raises(FutureDataError):
        news.visible("AAPL", T, T + timedelta(seconds=1), cutoff=T)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2025, 12, 31, tzinfo=UTC), T),  # starts before the fetched window
        (T, datetime(2026, 2, 14, tzinfo=UTC)),  # ends after it
    ],
)
def test_a_window_the_fetch_did_not_cover_is_missing_not_empty(tmp_path, start, end):
    news, _ = load(tmp_path, {"AAPL": [article(1, T)]})

    with pytest.raises(MissingCoverage):
        news.visible("AAPL", start, end, cutoff=datetime(2026, 3, 1, tzinfo=UTC))


def test_a_symbol_that_was_never_imported_is_missing(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T)]})

    with pytest.raises(MissingCoverage):
        news.visible("MSFT", T, T, cutoff=T)


def test_a_covered_window_with_no_articles_is_empty(tmp_path):
    news, _ = load(tmp_path, {"AAPL": []})

    assert news.visible("AAPL", T, T, cutoff=T) == []


def test_text_and_headline_are_capped_with_an_explicit_marker(tmp_path):
    news, _ = load(tmp_path, {"AAPL": [article(1, T, headline="H" * 5000, content="x" * 150_000)]})

    [record] = news.visible("AAPL", T, T, cutoff=T)
    assert len(record.text) == 100_000
    assert record.text.endswith("\n[truncated at 100000 characters]")
    assert len(record.headline) == 4096


def test_truncate_leaves_short_text_alone_and_fits_long_text_with_its_marker():
    assert truncate("abc", 10) == "abc"
    marker = "\n[truncated at 200000 characters]"
    assert truncate("x" * 200_001, 200_000) == "x" * (200_000 - len(marker)) + marker
    assert len(truncate("x" * 200_001, 200_000)) == 200_000


def test_an_article_without_a_headline_is_counted_not_stored(tmp_path):
    news, report = load(tmp_path, {"AAPL": [article(1, T, headline="  "), article(2, T)]})

    assert ids(news.visible("AAPL", T, T, cutoff=T)) == ["2"]
    assert [(s.symbol, s.articles, s.without_headline) for s in report.symbols] == [("AAPL", 1, 1)]


def test_reimporting_is_a_no_op_and_a_changed_article_conflicts(tmp_path):
    pages = {"AAPL": [article(1, T)]}
    db = tmp_path / "m.db"
    for _ in range(2):
        with closing(sqlite3.connect(db)) as connection:
            import_news_snapshot(connection, freeze(tmp_path, pages))
    changed = freeze(tmp_path / "other", {"AAPL": [article(1, T, content="edited")]})

    with closing(sqlite3.connect(db)) as connection, pytest.raises(NewsConflict):
        import_news_snapshot(connection, changed)
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM data_news").fetchone()[0] == 1


def test_an_expected_symbol_that_was_never_fetched_fails(tmp_path):
    with (
        closing(sqlite3.connect(tmp_path / "m.db")) as connection,
        pytest.raises(SourceError, match="never fetched MSFT"),
    ):
        import_news_snapshot(connection, freeze(tmp_path, {"AAPL": []}), expected=("AAPL", "MSFT"))


def test_explicit_symbols_import_only_those(tmp_path):
    news, report = load(tmp_path, {"AAPL": [article(1, T)], "KO": [article(2, T)]}, symbols=("KO",))

    assert report.left_out == ("AAPL",)
    with pytest.raises(MissingCoverage):
        news.visible("AAPL", T, T, cutoff=T)


def test_cli_import_news_without_a_snapshot_says_to_fetch_first(tmp_path, capsys):
    missing = tmp_path / "raw" / "alpaca-news" / "news-x"

    code = main(["import-news", "--snapshot", str(missing), "--db", str(tmp_path / "m.db")])

    assert code == 1
    assert "sources news --version news-x first." in capsys.readouterr().err
    assert not (tmp_path / "m.db").exists()


def test_cli_import_news_imports_and_reports(tmp_path, capsys):
    snapshot = freeze(tmp_path, {"AAPL": [article(1, T), article(2, T, headline="")]})

    code = main(
        [
            "import-news",
            "--snapshot",
            str(snapshot),
            "--db",
            str(tmp_path / "m.db"),
            "--symbols",
            "AAPL",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "import-news: AAPL 1 articles, 1 without a headline left out" in out
    assert "as alpaca-news-v1" in out

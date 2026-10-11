"""Share classes whose news Alpaca tags differently (BRK.B as BRK/B and BRK) keep their symbol."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from ..test_news import article, load

T = datetime(2026, 2, 2, 15, 0, tzinfo=UTC)
ALIASES = {"BRK/B": "BRK.B", "BRK": "BRK.B"}


def test_aliased_tags_are_stored_once_under_the_universe_symbol(tmp_path):
    both = article("both", T)  # tagged BRK/B and BRK, so fetched under each
    pages = {
        "BRK/B": [both, article("b-only", T + timedelta(hours=1))],
        "BRK": [both, article("plain", T + timedelta(hours=2))],
        "AAPL": [article("apple", T)],
    }

    news, report = load(tmp_path, pages, aliases=ALIASES)

    later = T + timedelta(days=1)
    stored = news.visible("BRK.B", T - timedelta(days=1), later, cutoff=later)
    assert sorted(r.record_id for r in stored) == ["b-only", "both", "plain"]
    assert {(s.symbol, s.articles) for s in report.symbols} == {("AAPL", 1), ("BRK.B", 3)}
    with closing(sqlite3.connect(tmp_path / "m.db")) as connection:
        symbols = {r[0] for r in connection.execute("SELECT DISTINCT symbol FROM data_news")}
        windows = {r[0] for r in connection.execute("SELECT symbol FROM data_news_coverage")}
    assert symbols == windows == {"AAPL", "BRK.B"}


def test_without_aliases_each_tag_stays_its_own_symbol(tmp_path):
    pages = {"BRK/B": [article("b", T)], "BRK": [article("p", T)]}

    _, report = load(tmp_path, pages)

    assert {s.symbol for s in report.symbols} == {"BRK/B", "BRK"}


def test_an_aliased_symbol_with_no_articles_still_records_its_window(tmp_path):
    news, report = load(tmp_path, {"BF": []}, aliases={"BF": "BF.B"})

    assert [(s.symbol, s.articles) for s in report.symbols] == [("BF.B", 0)]
    later = T + timedelta(days=1)
    assert news.visible("BF.B", T, later, cutoff=later) == []

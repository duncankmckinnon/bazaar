from datetime import date

import pytest
from bazaar_market.sources.universe import (
    ConfigError,
    in_universe,
    load_config,
    parse_sp500_start_end,
)

CSV = """ticker,start_date,end_date
FB,2013-12-23,2022-06-09
META,2022-06-09,
AAL,1996-01-02,1997-01-15
AAL,2015-03-23,2024-09-23
"""


def test_parse_reads_open_ended_membership_as_no_end_date():
    rows = parse_sp500_start_end(CSV)

    meta = [r for r in rows if r.ticker == "META"]
    assert [(m.start, m.end) for m in meta] == [(date(2022, 6, 9), None)]


def test_parse_keeps_each_spell_of_a_ticker_that_left_and_returned():
    rows = parse_sp500_start_end(CSV)

    assert [(r.start, r.end) for r in rows if r.ticker == "AAL"] == [
        (date(1996, 1, 2), date(1997, 1, 15)),
        (date(2015, 3, 23), date(2024, 9, 23)),
    ]


@pytest.mark.parametrize(
    ("ticker", "on", "want"),
    [
        ("FB", date(2022, 6, 8), True),
        ("FB", date(2022, 6, 9), False),  # the end date itself is already outside
        ("META", date(2022, 6, 8), False),
        ("META", date(2022, 6, 9), True),
        ("META", date(2026, 1, 2), True),
        ("AAL", date(1996, 6, 1), True),
        ("AAL", date(2005, 6, 1), False),  # the gap between two spells
        ("AAL", date(2016, 6, 1), True),
        ("ZZZZ", date(2022, 6, 8), False),
    ],
)
def test_in_universe_only_inside_a_membership_spell(ticker, on, want):
    assert in_universe(parse_sp500_start_end(CSV), ticker, on) is want


CONFIG = """
[period]
start = 2022-06-01
end = 2023-06-30

[sp500]
commit = "a2430f2"

[edgar]
forms = ["10-K", "10-K/A", "8-K"]
documents_since = 2021-06-01

[[company]]
ticker = "META"
cik = 1326801
news_symbols = ["META", "FB"]

[[company]]
ticker = "SIVB"
cik = 719739
"""


def test_load_config_reads_period_companies_and_forms(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(CONFIG)

    cfg = load_config(path)

    assert (cfg.period_start, cfg.period_end) == (date(2022, 6, 1), date(2023, 6, 30))
    assert cfg.sp500_commit == "a2430f2"
    assert cfg.edgar_forms == ("10-K", "10-K/A", "8-K")
    assert cfg.edgar_documents_since == date(2021, 6, 1)
    assert [(c.ticker, c.cik, c.news_symbols) for c in cfg.companies] == [
        ("META", 1326801, ("META", "FB")),
        ("SIVB", 719739, ("SIVB",)),
    ]


def test_load_config_rejects_a_company_without_an_sec_number(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(CONFIG.replace("cik = 719739\n", ""))

    with pytest.raises(ConfigError, match="SIVB"):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('forms = ["10-K", "10-K/A", "8-K"]', 'forms = "10-K"', "forms"),
        ("start = 2022-06-01", 'start = "2022-06-01"', "period.start"),
        ("end = 2023-06-30", "end = 2021-01-01", "before"),
        ('ticker = "SIVB"', 'ticker = "META"', "META"),
        ("[sp500]", "[sp500x]", "sp500"),
        ("documents_since = 2021-06-01", "documents_since = 20210601", "documents_since"),
    ],
)
def test_load_config_rejects_a_malformed_setting(tmp_path, old, new, message):
    assert old in CONFIG
    path = tmp_path / "sources.toml"
    path.write_text(CONFIG.replace(old, new))

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_load_config_rejects_a_config_with_no_companies(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(CONFIG.split("[[company]]")[0])

    with pytest.raises(ConfigError, match="company"):
        load_config(path)

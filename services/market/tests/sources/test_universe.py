from datetime import date
from pathlib import Path

import pytest
from bazaar_market.sources.universe import (
    ConfigError,
    in_universe,
    load_config,
    parse_sp500_start_end,
    read_universe,
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


UNIVERSE_TOML = (
    "[period]\nstart = 2025-07-01\nend = 2026-10-09\n[news]\nstart = 2026-06-01\n"
    '[universe]\ncsv = "u.csv"\nbenchmarks = ["SPY"]\nbars_only = ["SKYD"]\n'
    '[sp500]\ncommit = "c"\n[edgar]\nforms = ["10-K"]\ndocuments_since = 2025-10-01\n'
)
UNIVERSE_CSV = (
    "symbol,name,gics_sector,cik\nBRK.B,Berkshire Hathaway,Financials,1067983\n"
    "FISV,Fiserv,Financials,798354\n"
)


def universe_config(tmp_path, csv_text=UNIVERSE_CSV, toml=UNIVERSE_TOML):
    (tmp_path / "u.csv").write_text(csv_text)
    config = tmp_path / "sources.toml"
    config.write_text(toml)
    return config


def test_load_config_reads_companies_from_a_universe_csv_next_to_it(tmp_path):
    cfg = load_config(universe_config(tmp_path))

    assert [(c.ticker, c.cik, c.news_symbols) for c in cfg.companies] == [
        ("BRK.B", 1067983, ("BRK.B",)),
        ("FISV", 798354, ("FISV",)),
    ]
    assert cfg.news_start == date(2026, 6, 1)
    assert cfg.bar_tickers == ("BRK.B", "FISV", "SPY", "SKYD")


def test_without_universe_or_news_sections_nothing_changes(tmp_path):
    config = tmp_path / "sources.toml"
    config.write_text(
        '[period]\nstart = 2025-07-01\nend = 2026-10-09\n[sp500]\ncommit = "c"\n'
        '[edgar]\nforms = ["10-K"]\ndocuments_since = 2025-10-01\n'
        '[[company]]\nticker = "KO"\ncik = 21344\n'
    )

    cfg = load_config(config)

    assert (cfg.news_start, cfg.benchmarks, cfg.bars_only, cfg.bar_tickers) == (
        None,
        (),
        (),
        ("KO",),
    )


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("symbol,name,sector,cik\nKO,Coca-Cola,Consumer Staples,21344\n", "header"),
        ("symbol,name,gics_sector,cik\nKO,Coca-Cola,,21344\n", "sector"),
        ("symbol,name,gics_sector,cik\nKO,Coca-Cola,Consumer Staples,x\n", "CIK"),
        ("symbol,name,gics_sector,cik\nKO,A,S,1\nKO,A,S,1\n", "twice"),
        ("symbol,name,gics_sector,cik\n", "no companies"),
    ],
)
def test_a_bad_universe_csv_is_refused(tmp_path, csv_text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(universe_config(tmp_path, csv_text))


def test_a_news_start_outside_the_period_is_refused(tmp_path):
    toml = UNIVERSE_TOML.replace("start = 2026-06-01", "start = 2025-06-01")

    with pytest.raises(ConfigError, match="news.start"):
        load_config(universe_config(tmp_path, toml=toml))


def test_universe_and_listed_companies_cannot_be_mixed(tmp_path):
    toml = UNIVERSE_TOML + '[[company]]\nticker = "KO"\ncik = 21344\n'

    with pytest.raises(ConfigError, match="not both"):
        load_config(universe_config(tmp_path, toml=toml))


CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"


def test_the_example_universe_csv_is_placeholders_in_the_real_format():
    rows = read_universe(CONFIG_DIR / "sp500-universe.example.csv")

    assert [r.name for r in rows] == [
        "Example Industries",
        "Example Holdings Class B",
        "Example Software",
    ]


def test_universe_csv_replaces_the_configs_csv(tmp_path):
    other = tmp_path / "elsewhere.csv"
    other.write_text("symbol,name,gics_sector,cik\nKO,Coca-Cola,Consumer Staples,21344\n")

    cfg = load_config(universe_config(tmp_path), universe_csv=other)

    assert [c.ticker for c in cfg.companies] == ["KO"]
    assert cfg.universe_csv == other


def test_a_missing_universe_csv_says_it_is_not_committed(tmp_path):
    with pytest.raises(ConfigError, match="not committed"):
        load_config(universe_config(tmp_path), universe_csv=tmp_path / "missing.csv")


def test_the_fetch_requests_benchmark_bars_and_starts_news_at_news_start(tmp_path):
    import json

    import httpx
    from bazaar_market.sources.cli import main

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "news" in request.url.path:
            return httpx.Response(200, content=json.dumps({"news": [], "next_page_token": None}))
        return httpx.Response(200, content=json.dumps({"bars": {}, "next_page_token": None}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    keys = {"ALPACA_API_KEY": "id", "ALPACA_SECRET_KEY": "secret"}
    config, root = str(universe_config(tmp_path)), str(tmp_path / "raw")
    for command in ("bars", "news"):
        main(
            [command, "--config", config, "--root", root, "--version", "v1"], env=keys, http=client
        )

    bars = [r.url.params["symbols"] for r in seen if "bars" in r.url.path]
    news = [r.url.params for r in seen if "news" in r.url.path]
    assert bars == ["BRK.B", "FI", "FISV", "SPY", "SKYD"]  # FISV splits at the FI rename
    assert [p["symbols"] for p in news] == ["BRK.B", "FISV"]
    assert {p["start"] for p in news} == {"2026-06-01T00:00:00Z"}


@pytest.mark.parametrize(
    ("command", "flags", "message"),
    [
        ("import-bars", [], "needs --data-version"),
        ("import-bars", ["--data-version", "alpaca-bars-v2"], "needs --required-from"),
        ("import-news", [], "needs --data-version"),
        ("import-filings", [], "needs --data-version"),
    ],
)
def test_a_universe_config_import_must_name_its_versions(tmp_path, capsys, command, flags, message):
    from bazaar_market.sources.cli import main

    snapshot = tmp_path / "raw" / "x" / "v1"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text("{}")
    argv = [command, "--config", str(universe_config(tmp_path)), "--snapshot", str(snapshot)]

    code = main([*argv, "--db", str(tmp_path / "m.db"), *flags])

    assert code == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "m.db").exists()


def test_universe_renames_are_read_and_fetched_for_bars(tmp_path):
    toml = UNIVERSE_TOML.replace('bars_only = ["SKYD"]\n', "").replace(
        '[universe]\ncsv = "u.csv"\n',
        '[universe]\ncsv = "u.csv"\n'
        'renames = [{ ticker = "FISV2", symbol = "FISV", effective = 2026-10-06 }]\n',
    )

    cfg = load_config(universe_config(tmp_path, toml=toml))

    assert [(r.ticker, r.symbol, r.effective) for r in cfg.renames] == [
        ("FISV2", "FISV", date(2026, 10, 6))
    ]
    assert cfg.bar_tickers == ("BRK.B", "FISV", "SPY", "FISV2")


def test_a_rename_to_a_symbol_outside_the_universe_is_refused(tmp_path):
    toml = UNIVERSE_TOML.replace(
        '[universe]\ncsv = "u.csv"\n',
        '[universe]\ncsv = "u.csv"\n'
        'renames = [{ ticker = "SKYD", symbol = "PSKY", effective = 2026-10-06 }]\n',
    )

    with pytest.raises(ConfigError, match="not in the universe"):
        load_config(universe_config(tmp_path, toml=toml))


def test_news_aliases_replace_a_symbols_news_tags_and_map_back(tmp_path):
    toml = UNIVERSE_TOML.replace(
        '[universe]\ncsv = "u.csv"\n',
        '[universe]\ncsv = "u.csv"\nnews_aliases = { "BRK.B" = ["BRK/B", "BRK"] }\n',
    )

    cfg = load_config(universe_config(tmp_path, toml=toml))

    assert [c.news_symbols for c in cfg.companies] == [("BRK/B", "BRK"), ("FISV",)]
    assert cfg.news_aliases == {"BRK/B": "BRK.B", "BRK": "BRK.B"}


@pytest.mark.parametrize(
    "aliases",
    ['{ "XYZ" = ["X"] }', '{ "BRK.B" = [] }', '{ "BRK.B" = ["F"], "FISV" = ["F"] }'],
)
def test_bad_news_aliases_are_refused(tmp_path, aliases):
    toml = UNIVERSE_TOML.replace(
        '[universe]\ncsv = "u.csv"\n', f'[universe]\ncsv = "u.csv"\nnews_aliases = {aliases}\n'
    )

    with pytest.raises(ConfigError, match="news_aliases"):
        load_config(universe_config(tmp_path, toml=toml))


def test_no_real_constituent_list_is_committed_in_config():
    # The repository is public. Only the placeholder example may sit in config/.
    assert sorted(p.name for p in CONFIG_DIR.glob("*.csv")) == ["sp500-universe.example.csv"]

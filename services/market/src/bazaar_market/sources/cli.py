"""Command line for freezing source data to disk.

    uv run --env-file .env python -m bazaar_market.sources all
    uv run --env-file .env python -m bazaar_market.sources bars
    uv run python -m bazaar_market.sources import-bars --snapshot data/raw/alpaca-bars/<version>

Reads ALPACA_API_KEY and ALPACA_SECRET_KEY for news and bars. The EDGAR User-Agent is SEC_USER_AGENT, or
else edgar.user_agent in the config. Filing text is downloaded only when it names a contact address.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from ..filings import FILINGS_VERSION
from ..news import NEWS_VERSION
from .alpaca_bars import ticker_windows
from .bars_import import ALPACA_BARS_VERSION, import_bars_snapshot, weekdays
from .errors import SourceError
from .fetch import fetch_bars_range, fetch_edgar, fetch_news_range, fetch_universe
from .filings_import import document_window, import_filings_snapshot
from .news_import import import_news_snapshot
from .snapshot import SnapshotConflict
from .universe import SourcesConfig, load_config, read_universe
from .universe_import import BENCHMARK_NAMES, import_universe

DEFAULT_SEC_USER_AGENT = "bazaar-market-sources/0.1 (no contact declared)"


def _alpaca_headers(env: Mapping[str, str], command: str) -> dict[str, str]:
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(
            f"{command} needs ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment. "
            "Run it with: uv run --env-file .env python -m bazaar_market.sources " + command
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    http: httpx.Client | None = None,
    now: datetime | None = None,
) -> int:
    """Run one command. A source or snapshot failure prints one line and exits 1."""
    try:
        return _run(argv, env=env, http=http, now=now)
    except (SourceError, SnapshotConflict) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _run(
    argv: list[str] | None,
    *,
    env: Mapping[str, str] | None,
    http: httpx.Client | None,
    now: datetime | None,
) -> int:
    parser = argparse.ArgumentParser(prog="bazaar_market.sources", description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "universe",
            "edgar",
            "news",
            "capture-news",
            "all",
            "bars",
            "import-bars",
            "import-news",
            "import-filings",
            "import-universe",
        ],
    )
    parser.add_argument("--config", default="config/demo-sources.toml")
    parser.add_argument("--root", default="data/raw")
    parser.add_argument("--version", help="snapshot version, default is the current UTC minute")
    parser.add_argument("--days", type=int, default=3, help="capture-news: trailing days to save")
    parser.add_argument("--feed", default="sip", help="bars: Alpaca feed, sip or iex")
    parser.add_argument("--snapshot", type=Path, help="import-bars: the frozen bars version folder")
    parser.add_argument("--db", type=Path, default=Path("data/market.sqlite3"), help="import-bars")
    parser.add_argument(
        "--symbols", help="import-bars: comma-separated tickers to import; the rest are left out"
    )
    parser.add_argument(
        "--data-version", help="import-bars, import-news and import-filings: data version to store"
    )
    parser.add_argument(
        "--universe-csv",
        type=Path,
        help="the universe CSV, replacing the config's [universe] csv (it is not committed)",
    )
    parser.add_argument("--bundle", help="import-universe: the bundle to store the universe for")
    parser.add_argument(
        "--required-from",
        type=date.fromisoformat,
        help="import-bars: first day of a window in which every weekday must have a bar for "
        "each required symbol (exchange holidays are not removed)",
    )
    parser.add_argument(
        "--required-to",
        type=date.fromisoformat,
        help="import-bars: last day of a window in which every weekday must have a bar for "
        "each required symbol (exchange holidays are not removed)",
    )
    parser.add_argument(
        "--required-symbols",
        help="import-bars: comma-separated tickers the required window applies to; "
        "default --symbols",
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    if args.command.startswith("import-"):
        _require_explicit_versions(args)
    if args.command == "import-bars":
        return _import_bars(args)
    if args.command == "import-news":
        return _import_news(args)
    if args.command == "import-filings":
        return _import_filings(args)
    if args.command == "import-universe":
        return _import_universe(args)
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    version = args.version or now.astimezone(UTC).strftime("%Y-%m-%dT%H%MZ")
    cfg = _config(args)
    root = Path(args.root)
    needs_news = args.command in ("news", "capture-news", "all")
    needs_keys = needs_news or args.command == "bars"
    news_headers = _alpaca_headers(env, args.command) if needs_keys else None
    http = http or httpx.Client(timeout=60)

    if args.command in ("universe", "all"):
        members = fetch_universe(cfg, root, http)
        print(f"universe: {len(members)} membership spells frozen")
    if args.command in ("edgar", "all"):
        agent = env.get("SEC_USER_AGENT") or cfg.edgar_user_agent or DEFAULT_SEC_USER_AGENT
        summary = fetch_edgar(
            cfg,
            root,
            http,
            version=version,
            user_agent=agent,
            min_interval=float(env.get("SEC_REQUEST_INTERVAL", "0.2")),
        )
        for ticker, counts in summary.items():
            print(f"edgar: {ticker} {counts['filings']} filings, {counts['documents']} documents")
        if "@" not in agent:
            print("edgar: filing text skipped. Set SEC_USER_AGENT to a name and contact address.")
    if needs_news:
        if args.command == "capture-news":
            today = now.astimezone(UTC).date()
            start, end, until = today - timedelta(days=args.days - 1), today, now
        else:
            start, end, until = cfg.news_start or cfg.period_start, cfg.period_end, None
        counts = fetch_news_range(
            cfg,
            root,
            http,
            version=version,
            start=start,
            end=end,
            headers=news_headers,
            until=until,
        )
        for symbol, count in counts.items():
            print(f"news: {symbol} {count} articles from {start} to {end}")
    if args.command == "bars":
        counts = fetch_bars_range(
            cfg, root, http, version=version, feed=args.feed, headers=news_headers
        )
        for ticker, count in counts.items():
            print(f"bars: {ticker} {count} daily bars")
    print(f"snapshot version {version} under {root}")
    return 0


def _config(args: argparse.Namespace) -> SourcesConfig:
    return load_config(Path(args.config), universe_csv=args.universe_csv)


def _expected_tickers(cfg: SourcesConfig) -> tuple[str, ...]:
    """Every ticker the bars fetch requests for the config, with FI and FISV split by date."""
    return tuple(
        w.ticker for w in ticker_windows(cfg.bar_tickers, cfg.period_start, cfg.period_end)
    )


def _missing_snapshot(args: argparse.Namespace, fetch_command: str) -> bool:
    """True, after printing how to fetch one, when --snapshot holds no frozen snapshot."""
    if args.snapshot is None:
        raise SystemExit(f"{args.command} needs --snapshot data/raw/<source>/<version>")
    if (args.snapshot / "manifest.json").is_file():
        return False
    print(
        f"No snapshot at {args.snapshot}. Run: uv run --env-file .env python -m "
        f"bazaar_market.sources {fetch_command} --version {args.snapshot.name} first.",
        file=sys.stderr,
    )
    return True


def _symbols(args: argparse.Namespace) -> tuple[str, ...] | None:
    return tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else None


def _import_news(args: argparse.Namespace) -> int:
    if _missing_snapshot(args, "news"):
        return 1
    symbols = _symbols(args)
    expected = None
    cfg = _config(args)
    if not symbols:
        expected = tuple(dict.fromkeys(s for c in cfg.companies for s in c.news_symbols))
    data_version = args.data_version or NEWS_VERSION
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db)) as connection:
        report = import_news_snapshot(
            connection,
            args.snapshot,
            data_version=data_version,
            expected=expected,
            symbols=symbols,
            aliases=cfg.news_aliases,
        )
    for s in report.symbols:
        dropped = (
            f", {s.without_headline} without a headline left out" if s.without_headline else ""
        )
        print(f"import-news: {s.symbol} {s.articles} articles{dropped}")
    if report.left_out:
        print(f"import-news: left out, not imported: {', '.join(report.left_out)}")
    print(f"import-news: imported {args.snapshot} into {args.db} as {data_version}")
    return 0


def _import_filings(args: argparse.Namespace) -> int:
    if _missing_snapshot(args, "edgar"):
        return 1
    cfg = _config(args)
    symbols = _symbols(args)
    companies = {c.ticker: c.cik for c in cfg.companies if not symbols or c.ticker in symbols}
    unknown = sorted(set(symbols or ()) - set(companies))
    if unknown:
        raise SourceError(f"not in the config: {', '.join(unknown)}")
    window = document_window(cfg.edgar_documents_since, cfg.period_end)
    data_version = args.data_version or FILINGS_VERSION
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db)) as connection:
        report = import_filings_snapshot(
            connection, args.snapshot, companies, window, data_version=data_version
        )
    for c in report:
        print(
            f"import-filings: {c.symbol} (CIK {c.cik}) {c.served} 10-K/10-Q served, "
            f"{c.truncated} truncated, {len(c.excluded)} left out without an XBRL period, "
            f"{c.outside_window} outside the document window"
        )
        for e in c.excluded:
            print(f"import-filings:   left out {e.form} {e.accession}: {e.reason}")
    print(f"import-filings: imported {args.snapshot} into {args.db} as {data_version}")
    return 0


def _require_explicit_versions(args: argparse.Namespace) -> None:
    """A universe config (v2) must name its data version, and bars their required window.

    The defaults are the v1 versions, so a v2 import without them would land silently in v1.
    """
    if _config(args).universe_csv is None:
        return
    if not args.data_version:
        raise SourceError(f"{args.command} with {args.config} needs --data-version")
    if args.command in ("import-bars", "import-universe") and not (
        args.required_from and args.required_to
    ):
        raise SourceError(
            f"{args.command} with {args.config} needs --required-from and --required-to"
        )


def _required(args: argparse.Namespace) -> dict[str, set[date]] | None:
    """The sessions that must have bars, from the --required-* flags, or None for the default."""
    window = (args.required_from, args.required_to)
    if window == (None, None):
        if args.required_symbols:
            raise SourceError("--required-symbols needs --required-from and --required-to")
        return None
    if None in window or args.required_from > args.required_to:
        raise SourceError("give --required-from and --required-to, from no later than to")
    raw = args.required_symbols or args.symbols
    if not raw:
        raise SourceError("--required-from and --required-to need --required-symbols or --symbols")
    sessions = weekdays(args.required_from, args.required_to)
    return {s.strip(): sessions for s in raw.split(",") if s.strip()}


def _import_bars(args: argparse.Namespace) -> int:
    if _missing_snapshot(args, "bars"):
        return 1
    required = _required(args)
    symbols = _symbols(args)
    expected = None if symbols else _expected_tickers(_config(args))
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db)) as connection:
        report = import_bars_snapshot(
            connection,
            args.snapshot,
            data_version=args.data_version or ALPACA_BARS_VERSION,
            expected=expected,
            symbols=symbols,
            required=required,
            renames=_config(args).renames,
        )
    for r in report.renamed:
        dropped = (
            f", {r.dropped_before} earlier {r.rename.ticker} bars left out"
            if r.dropped_before
            else ""
        )
        print(
            f"import-bars: renamed {r.rename.ticker} -> {r.rename.symbol} from "
            f"{r.rename.effective}: {r.mapped} bars stored as {r.rename.symbol}{dropped}"
        )
    if required is not None:
        print(
            f"import-bars: every weekday {args.required_from} to {args.required_to} present for "
            f"{', '.join(required)}"
        )
    for c in report.coverage:
        print(f"import-bars: {c.ticker} {c.bars} bars, {c.first} to {c.last}")
    if report.left_out:
        print(f"import-bars: left out, not imported: {', '.join(report.left_out)}")
    print(
        f"import-bars: imported {args.snapshot} into {args.db} as "
        f"{args.data_version or ALPACA_BARS_VERSION}"
    )
    return 0


def _import_universe(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if cfg.universe_csv is None:
        raise SourceError(
            f"import-universe needs a config with a [universe] csv, not {args.config}"
        )
    if not args.bundle:
        raise SourceError("import-universe needs --bundle")
    if len(cfg.benchmarks) != 1 or cfg.benchmarks[0] not in BENCHMARK_NAMES:
        raise SourceError(f"import-universe needs one named benchmark, got {cfg.benchmarks}")
    [benchmark] = cfg.benchmarks
    report = import_universe(
        args.db,
        args.bundle,
        read_universe(cfg.universe_csv),
        benchmark=benchmark,
        benchmark_name=BENCHMARK_NAMES[benchmark],
        bars_version=args.data_version,
        required_from=args.required_from,
        required_to=args.required_to,
    )
    for e in report.excluded:
        last = f", last bar {e.last_bar}" if e.last_bar else ""
        print(f"import-universe: excluded {e.symbol}: {e.reason}{last}")
    print(
        f"import-universe: {args.bundle} stores {report.members} symbols plus {benchmark}, "
        f"{len(report.excluded)} excluded; every one of {len(report.sessions)} sessions "
        f"{args.required_from} to {args.required_to} covered"
    )
    return 0

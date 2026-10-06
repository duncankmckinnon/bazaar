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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .alpaca_bars import ticker_windows
from .bars_import import ALPACA_BARS_VERSION, import_bars_snapshot
from .errors import SourceError
from .fetch import fetch_bars_range, fetch_edgar, fetch_news_range, fetch_universe
from .snapshot import SnapshotConflict
from .universe import load_config

DEFAULT_SEC_USER_AGENT = "bazaar-market-sources/0.1 (no contact declared)"


def _alpaca_headers(env: Mapping[str, str]) -> dict[str, str]:
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("news needs ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment")
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
        choices=["universe", "edgar", "news", "capture-news", "all", "bars", "import-bars"],
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
        "--data-version", default=ALPACA_BARS_VERSION, help="import-bars: data version to store"
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    if args.command == "import-bars":
        return _import_bars(args)
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    version = args.version or now.astimezone(UTC).strftime("%Y-%m-%dT%H%MZ")
    cfg = load_config(Path(args.config))
    root = Path(args.root)
    needs_news = args.command in ("news", "capture-news", "all")
    news_headers = _alpaca_headers(env) if needs_news or args.command == "bars" else None
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
            start, end, until = cfg.period_start, cfg.period_end, None
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


def _expected_tickers(config: Path) -> tuple[str, ...]:
    """Every ticker the bars fetch requests for the config, with FI and FISV split by date."""
    cfg = load_config(config)
    tickers = tuple(c.ticker for c in cfg.companies)
    return tuple(w.ticker for w in ticker_windows(tickers, cfg.period_start, cfg.period_end))


def _import_bars(args: argparse.Namespace) -> int:
    if args.snapshot is None:
        raise SystemExit("import-bars needs --snapshot data/raw/alpaca-bars/<version>")
    if not (args.snapshot / "manifest.json").is_file():
        print(
            f"No snapshot at {args.snapshot}. Run: uv run --env-file .env python -m "
            f"bazaar_market.sources bars --version {args.snapshot.name} first.",
            file=sys.stderr,
        )
        return 1
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db)) as connection:
        symbols = tuple(s.strip() for s in args.symbols.split(",")) if args.symbols else None
        expected = None if symbols else _expected_tickers(Path(args.config))
        report = import_bars_snapshot(
            connection,
            args.snapshot,
            data_version=args.data_version,
            expected=expected,
            symbols=symbols,
        )
    for c in report.coverage:
        print(f"import-bars: {c.ticker} {c.bars} bars, {c.first} to {c.last}")
    if report.left_out:
        print(f"import-bars: left out, not imported: {', '.join(report.left_out)}")
    print(f"import-bars: imported {args.snapshot} into {args.db} as {args.data_version}")
    return 0

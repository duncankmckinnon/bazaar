"""Command line for freezing source data to disk.

    uv run --env-file .env python -m bazaar_market.sources all

Reads ALPACA_API_KEY and ALPACA_SECRET_KEY for news. SEC_USER_AGENT is optional. Give it a
name and contact address to also download filing text.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .fetch import fetch_edgar, fetch_news_range, fetch_universe
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
    today: date | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="bazaar_market.sources", description=__doc__)
    parser.add_argument("command", choices=["universe", "edgar", "news", "capture-news", "all"])
    parser.add_argument("--config", default="config/demo-sources.toml")
    parser.add_argument("--root", default="data/raw")
    parser.add_argument("--version", help="snapshot version, default is the current UTC minute")
    parser.add_argument("--days", type=int, default=3, help="capture-news: trailing days to save")
    args = parser.parse_args(argv)

    env = os.environ if env is None else env
    today = today or datetime.now(UTC).date()
    version = args.version or datetime.now(UTC).strftime("%Y-%m-%dT%H%MZ")
    cfg = load_config(Path(args.config))
    root = Path(args.root)
    needs_news = args.command in ("news", "capture-news", "all")
    news_headers = _alpaca_headers(env) if needs_news else None
    http = http or httpx.Client(timeout=60, follow_redirects=True)

    if args.command in ("universe", "all"):
        members = fetch_universe(cfg, root, http)
        print(f"universe: {len(members)} membership spells frozen")
    if args.command in ("edgar", "all"):
        agent = env.get("SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT
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
            start, end = today - timedelta(days=args.days - 1), today
        else:
            start, end = cfg.period_start, cfg.period_end
        counts = fetch_news_range(
            cfg, root, http, version=version, start=start, end=end, headers=news_headers
        )
        for symbol, count in counts.items():
            print(f"news: {symbol} {count} articles from {start} to {end}")
    print(f"snapshot version {version} under {root}")
    return 0

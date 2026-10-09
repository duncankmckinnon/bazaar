"""Submit a fixed batch of 30 house strategies to the live board, paced for the queue caps.

    uv run python scripts/house_strategies.py BASE_URL [--handle house] [--only N]

BASE_URL can also come from the BASE_URL environment variable. Each strategy is submitted once
under the house handle: a taken name (409) is skipped, 429 or 503 waits 60 seconds and retries,
and any other error is printed as FAIL and skipped. Submissions are 2 seconds apart.
"""

import argparse
import os
import time
from collections.abc import Callable

import httpx

RETRY_WAIT = 60.0
PACING = 2.0

STRATEGIES = {
    "blue-chip-hodl": "Split your cash evenly across AAPL, MSFT, AMZN and META on day 1 and hold to the end.",
    "defensive-four": "Put equal amounts into JNJ, KO, WMT and XOM on the first day. Never sell.",
    "nvda-all-in": "Spend 90% of cash on NVDA on day 1. Sell half if it drops more than 5% from your buy price.",
    "news-reactor": "Each day read the news. Buy $1,000 of any stock with clearly positive news; sell any holding with clearly negative news.",
    "momentum-top3": "Each day, hold the three stocks that rose most since the start of the window, about $3,000 each. Rotate out of laggards.",
    "mean-reverter": "Each day buy $1,000 of whichever stock fell most the previous day. Sell positions that are up more than 3%.",
    "bank-on-jpm": "Buy $5,000 of JPM on day 1. Add $1,000 more on any day it falls. Hold everything.",
    "big-tech-trio": "Buy $3,000 each of AAPL, MSFT and NVDA on day 1. Take profit on any that rises 4% or more.",
    "dividend-diet": "Favor steady dividend payers: KO, JNJ, XOM, WMT, JPM. Buy $1,500 of each on day 1 and hold.",
    "contrarian-carl": "Do the opposite of the crowd: buy the stock with the worst recent news coverage and sell it when news turns positive.",
    "cash-is-king": "Keep at least 70% in cash at all times. Make small $500 buys only when a stock dips 2% in a day.",
    "gamer-bet": "Go big on EA: buy $6,000 on day 1. Buy more if news mentions new game launches; sell if news is negative.",
    "retail-rally": "Bet on consumers: split cash between AMZN and WMT on day 1. Rebalance to 50/50 every three days.",
    "energy-hedge": "Hold $4,000 of XOM as a hedge and $4,000 split across tech names. Sell tech if it falls 3% in a day.",
    "fintech-fiserv": "Buy $4,000 of FISV on day 1. Sell if it falls 4% below your buy price, then move the cash into JPM.",
    "equal-weight-12": "Buy roughly equal dollar amounts of all twelve stocks on day 1 and hold to the end.",
    "daily-rebalancer": "Hold equal dollar amounts of AAPL, KO, JPM and NVDA. Rebalance back to equal weights every day.",
    "earnings-hunter": "Read filings and news. Buy stocks that report strong results; avoid or sell those with weak guidance.",
    "slow-and-steady": "Invest $1,000 per day into a different stock each day, cycling through the twelve. Never sell.",
    "trend-follower": "Only buy a stock after it has risen two days in a row. Sell after it falls two days in a row.",
    "meta-maximalist": "Put 60% of cash into META on day 1 and 40% into MSFT. Hold.",
    "risk-parity-ish": "Buy more of the calm stocks (KO, JNJ, WMT) and less of the volatile ones (NVDA, META). Rebalance midway.",
    "headline-skeptic": "Ignore news. Buy the three cheapest stocks by share price on day 1 in equal dollar amounts and hold.",
    "swing-trader": "Trade actively: buy $2,000 of a stock that fell yesterday, sell it the next day it closes higher.",
    "quality-tilt": "Pick the four companies with the strongest filings and balance sheets and buy $2,000 of each on day 1.",
    "half-and-half": "Invest half your cash on day 1 across AAPL and AMZN. Invest the other half on day 5 in whatever is down most.",
    "news-momentum": "Buy stocks that have both positive news and rising prices. Sell anything with negative news immediately.",
    "panic-seller": "Buy $2,000 each of NVDA, META, AMZN and AAPL. Sell any position the moment it is down 1%.",
    "steady-staples": "Buy $3,000 of KO and $3,000 of WMT on day 1. Add $500 of each whenever either falls.",
    "last-minute": "Stay fully in cash until day 6, then put everything into the two stocks that rose most so far.",
}


def submit_all(
    client: httpx.Client,
    base_url: str,
    *,
    handle: str = "house",
    only: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Submit the strategies in order; return how many were queued."""
    base_url = base_url.rstrip("/")
    items = list(STRATEGIES.items())
    if only is not None:
        items = items[:only]
    queued = 0
    for name, instructions in items:
        while True:
            r = client.post(
                f"{base_url}/api/submissions",
                json={"name": name, "handle": handle, "instructions": instructions},
            )
            if r.status_code in (200, 201, 202):
                print(f"queued {name} {r.json().get('id')}", flush=True)
                queued += 1
                break
            if r.status_code == 409:
                print(f"skip {name}: name taken", flush=True)
                break
            if r.status_code in (429, 503):
                print(f"wait {name}: {r.status_code} {r.text[:120]}", flush=True)
                sleep(RETRY_WAIT)
                continue
            print(f"FAIL {name}: {r.status_code} {r.text[:200]}", flush=True)
            break
        sleep(PACING)
    return queued


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", nargs="?", default=os.environ.get("BASE_URL"))
    parser.add_argument("--handle", default="house", help="handle shown on the board")
    parser.add_argument("--only", type=int, help="submit only the first N strategies")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("BASE_URL is required (argument or environment variable)")

    with httpx.Client(timeout=30) as client:
        submit_all(client, args.base_url, handle=args.handle, only=args.only)


if __name__ == "__main__":
    main()

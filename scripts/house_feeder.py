"""Keep the live board busy with fresh house strategies, without crowding out attendees.

    uv run python scripts/house_feeder.py BASE_URL [--depth 4] [--max 80] [--handle house]

BASE_URL can also come from the BASE_URL environment variable. Submits a new generated strategy
whenever fewer than --depth submissions are queued or running on the board, until --max have been
queued. A taken name is skipped at once; 429 waits 5 minutes; any other error waits 30 seconds.
Stop it any time with Ctrl-C.
"""

import argparse
import itertools
import os
import random
import time
from collections.abc import Callable, Iterator

import httpx

BUSY_WAIT = 10.0
RATE_LIMIT_WAIT = 300.0
ERROR_WAIT = 30.0
PACING = 3.0

STOCKS = ["AAPL", "AMZN", "EA", "FISV", "JNJ", "JPM", "KO", "META", "MSFT", "NVDA", "WMT", "XOM"]

TEMPLATES = [
    ("{a}-{b}-pair", "Split cash evenly between {A} and {B} on day 1 and hold both to the end."),
    (
        "{a}-dip-buyer",
        "Buy $2,000 of {A} every day it closes lower than the day before. Never sell.",
    ),
    (
        "{a}-stop-loss",
        "Put $6,000 into {A} on day 1. Sell everything if it falls 3% below the buy price.",
    ),
    (
        "{a}-news-trader",
        "Read the news about {A} each day. Buy $1,500 on good news and sell half on bad news.",
    ),
    (
        "{a}-{b}-rotation",
        "Hold {A} for the first five days, then sell it and put everything into {B}.",
    ),
    (
        "{a}-take-profit",
        "Buy $5,000 of {A} on day 1 and sell it as soon as it is up 2%. Then buy {B} with the cash.",
    ),
    ("{a}-ladder", "Buy $1,000 of {A} on each of the first five days, then hold."),
    (
        "{a}-{b}-{c}-basket",
        "Buy equal dollar amounts of {A}, {B} and {C} on day 1. Rebalance to equal weights on day 5.",
    ),
    (
        "{a}-skeptic",
        "Avoid {A} completely. Spread your cash evenly over the other eleven stocks on day 1.",
    ),
    ("{a}-momentum", "Buy {A} only on days after it rose. Sell all of it on days after it fell."),
]


def strategies() -> Iterator[tuple[str, str]]:
    """Generated (name, instructions) pairs, the same sequence on every run."""
    seen = set()
    rng = random.Random(20261008)
    combos = list(itertools.permutations(STOCKS, 3))
    rng.shuffle(combos)
    for (a, b, c), (name_t, text_t) in zip(combos, itertools.cycle(TEMPLATES)):
        names = {"a": a.lower(), "b": b.lower(), "c": c.lower(), "A": a, "B": b, "C": c}
        name = name_t.format(**names)
        if name in seen or len(name) > 40:
            continue
        seen.add(name)
        yield name, text_t.format(**names)


def busy_count(client: httpx.Client, base_url: str) -> int:
    rows = client.get(f"{base_url}/api/board").json()["rows"]
    return sum(r["status"] in ("queued", "running") for r in rows)


def feed(
    client: httpx.Client,
    base_url: str,
    *,
    depth: int = 4,
    limit: int = 80,
    handle: str = "house",
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Submit until `limit` are queued, never with `depth` or more already busy."""
    base_url = base_url.rstrip("/")
    sent = 0
    gen = strategies()
    while sent < limit:
        if busy_count(client, base_url) >= depth:
            sleep(BUSY_WAIT)
            continue
        name, text = next(gen)
        r = client.post(
            f"{base_url}/api/submissions",
            json={"name": name, "handle": handle, "instructions": text},
        )
        if r.status_code in (200, 201, 202):
            sent += 1
            print(f"{time.strftime('%H:%M:%S')} queued {sent}/{limit} {name}", flush=True)
        elif r.status_code == 409 or "name is taken" in r.text:
            print(f"skip {name}: taken", flush=True)
            continue
        elif r.status_code == 429:
            print(
                f"{time.strftime('%H:%M:%S')} rate limited: {r.text[:120]}; waiting 5 min",
                flush=True,
            )
            sleep(RATE_LIMIT_WAIT)
        else:
            print(f"FAIL {name}: {r.status_code} {r.text[:200]}", flush=True)
            sleep(ERROR_WAIT)
        sleep(PACING)
    print("done", flush=True)
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", nargs="?", default=os.environ.get("BASE_URL"))
    parser.add_argument("--depth", type=int, default=4, help="most queued or running at once")
    parser.add_argument("--max", type=int, default=80, help="stop after this many are queued")
    parser.add_argument("--handle", default="house", help="handle shown on the board")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("BASE_URL is required (argument or environment variable)")

    with httpx.Client(timeout=30) as client:
        feed(client, args.base_url, depth=args.depth, limit=args.max, handle=args.handle)


if __name__ == "__main__":
    main()

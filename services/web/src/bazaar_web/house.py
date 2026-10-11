"""House strategies for the always-on house lane.

strategies() and its tables are moved unchanged from scripts/house_feeder.py (PR #81), so the
lane runs the same sequence the laptop feeder did. The lane walks it with a persisted cursor and
names each run house-<slug>-<cycle>; the cycle goes up each time the whole list has been used.
"""

import itertools
import random
from collections.abc import Iterator
from functools import cache

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


@cache
def catalog() -> tuple[tuple[str, str], ...]:
    """Every house strategy, in order."""
    return tuple(strategies())


def house_name(slug: str, cycle: int) -> str:
    return f"house-{slug}-{cycle}"

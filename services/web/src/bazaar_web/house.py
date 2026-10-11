"""House strategies for the always-on house lane.

The catalog follows the data bundle (catalog_for):
- a bundle with a universe [{symbol, name, sector}] (demo-bundle-v2, the S&P 500) gets
  universe_catalog: sector strategies, strategies over a fixed list of well-known large caps
  that are in the universe, and benchmark-relative ones;
- a bundle without one (demo-bundle-v1) gets the original 12-stock catalog(), moved unchanged
  from scripts/house_feeder.py (PR #81);
- no bundle from the route yet gives None: the lane waits rather than run stocks that may not
  be in the deploy's universe.
Nothing here reads prices, returns or volume: the agent screens and quotes at run time, so the
templates name sectors and companies, never numbers chosen from the window. The order is fixed.
The lane walks the catalog with a persisted cursor and names each run house-<slug>-<cycle>.
"""

import itertools
import random
import re
from collections.abc import Iterator
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bazaar_web.bundle import Bundle

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


Catalog = tuple[tuple[str, str], ...]


@cache
def catalog() -> Catalog:
    """The original 12-stock strategies, in order (for bundles without a universe)."""
    return tuple(strategies())


# Well-known mega and large caps, written down by hand as config, without looking at the
# window's prices or returns. Only those in the bundle's universe are used.
LARGE_CAPS = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK.B", "JPM", "V", "UNH",
    "XOM", "JNJ", "WMT", "PG", "HD", "KO", "MA", "LLY", "AVGO", "COST",
)  # fmt: skip

SECTOR_TEMPLATES = (
    (
        "{s}-top-gainers",
        (
            "Each day, screen {S} for the 3 biggest 5-day gainers and hold them equally, "
            "about $3,000 each. Sell any that drops out of the top 3."
        ),
    ),
    (
        "{s}-dip-buyers",
        (
            "Each day, buy $1,000 each of the 2 worst 1-day losers in {S}. Sell each position "
            "after holding it 3 trading days."
        ),
    ),
    (
        "{s}-steady",
        (
            "On day 1, put $8,000 into the 4 most traded (by volume) stocks in {S}, equal "
            "weights, and hold to the end."
        ),
    ),
)
SECTOR_BENCHMARK_TEMPLATES = (
    (
        "{s}-vs-{b}",
        (
            "Each day, compare the {S} sector's average 1-day change with {B}. If {S} beat "
            "{B}, hold its 3 strongest names; otherwise hold {B}."
        ),
    ),
)
NAME_TEMPLATES = (
    ("{a}-hold", "Put $6,000 into {A} on day 1 and hold to the end. Keep the rest in cash."),
    (
        "{a}-news",
        (
            "Read the news about {A} each day. Buy $1,500 on good news and sell half of the "
            "position on bad news."
        ),
    ),
    ("{a}-{b}-pair", "Split cash evenly between {A} and {B} on day 1 and hold both to the end."),
)
BENCHMARK_TEMPLATES = (
    (
        "beat-{b}",
        (
            "Hold {B} with all your cash. Switch to cash after any 2% drop from its highest "
            "close, and buy back the day after it rises."
        ),
    ),
    ("{b}-plus-{a}", "Put $7,000 into {B} and $3,000 into {A} on day 1 and hold both."),
)


def slug(text: str, limit: int = 14) -> str:
    """Lowercase letters, digits and dashes, at most limit characters."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


@cache
def universe_catalog(
    universe: tuple[tuple[str, str | None], ...], benchmark: str | None
) -> Catalog:
    """The strategies for a universe of (symbol, sector) pairs, in a fixed order.

    Groups are interleaved (sector, large name, benchmark, ...) so the lane mixes them. Each
    group is template order x sorted inputs. Only universe symbols and the benchmark appear.
    """
    symbols = {symbol for symbol, _ in universe}
    sectors = sorted({sector for _, sector in universe if sector})
    names = [s for s in LARGE_CAPS if s in symbols]
    by_sector = [
        (t_slug.format(s=slug(sector), b=slug(benchmark or "")),
         t_text.format(S=sector, B=benchmark or ""))
        for sector in sectors
        for t_slug, t_text in SECTOR_TEMPLATES + (SECTOR_BENCHMARK_TEMPLATES if benchmark else ())
    ]  # fmt: skip
    pairs = list(zip(names, names[1:] + names[:1], strict=True)) if len(names) > 1 else []
    by_name = [
        (t_slug.format(a=slug(a, 6), b=slug(b, 6)), t_text.format(A=a, B=b))
        for t_slug, t_text in NAME_TEMPLATES
        for a, b in (pairs if "{b}" in t_slug else [(n, n) for n in names])
    ]
    by_benchmark = (
        [
            (t_slug.format(a=slug(a, 6), b=slug(benchmark, 6)), t_text.format(A=a, B=benchmark))
            for t_slug, t_text in BENCHMARK_TEMPLATES
            for a in (names if "{a}" in t_slug else names[:1])
        ]
        if benchmark
        else []
    )
    seen: set[str] = set()
    ordered = []
    for group in itertools.zip_longest(by_sector, by_name, by_benchmark):
        for item in group:
            if item is not None and item[0] not in seen:
                seen.add(item[0])
                ordered.append(item)
    return tuple(ordered)


def catalog_for(bundle: "Bundle | None") -> Catalog | None:
    """The catalog for this bundle; None (the lane waits) while there is no bundle."""
    if bundle is None:
        return None
    if not bundle.sectors:
        return catalog()
    return universe_catalog(bundle.sectors, bundle.benchmark)


def house_name(slug: str, cycle: int) -> str:
    return f"house-{slug}-{cycle}"

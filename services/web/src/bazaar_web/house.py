"""The house lane's strategy factory (v8 F2): proposals from the strategist, checked here.

The lane no longer walks a fixed catalog. When it runs low it asks the strategist
(bazaar_agent.strategist, F1) for a batch: brief() ranks this window's strategies from
Logfire, and propose() writes new ones from the best and worst. Each proposal is checked again
here before it is queued (check_proposal), and its normalized text must match no submission or
earlier proposal, so the lane never runs the same instructions twice. Any strategist failure
means the lane waits; there is no catalog to fall back to.
"""

import re
import string
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bazaar_web.bundle import Bundle

PROPOSE_K = 6  # proposals asked for per strategist call
NAME = re.compile(r"[a-z0-9-]{3,40}")
INSTRUCTIONS_LEN = range(20, 601)
# Upper-case ticker-like tokens (AAPL, BRK.B, V). Prose in proposals is lower case apart from
# these common words, which are never taken for tickers.
TICKER = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b")
NOT_TICKERS = frozenset({"A", "I", "AI", "ETF", "ETFS", "US", "USD", "CEO", "EPS", "IPO", "P", "S"})


class Proposal(Protocol):
    """bazaar_agent.strategist.Proposal, as the lane reads it."""

    name: str
    instructions: str
    parents: list[str]
    idea: str


class Strategist(Protocol):
    """The two calls of bazaar_agent.strategist (the agreed F1/F2 contract). Both are
    synchronous, safe from a worker thread, and raise StrategistError on any failure."""

    def brief(
        self, *, bundle_id: str, since: Any, environment: str = ..., top: int = ...,
        bottom: int = ..., exclude_names: frozenset[str] = ..., data_version: str | None = ...,
    ) -> Any: ...  # fmt: skip

    def propose(
        self, *, brief: Any, k: int, generation: int, instructions_by_name: dict[str, str],
        existing_instructions: list[str], universe: list[dict[str, Any]], benchmark: str | None,
        trading_days: int | None = ...,
    ) -> list[Proposal]: ...  # fmt: skip


def import_strategist() -> Strategist:
    """The real strategist, imported only when the lane first needs it (F1, not on main yet)."""
    from bazaar_agent import strategist

    return strategist  # type: ignore[return-value]


def normalize(text: str) -> str:
    """Instructions as compared for duplicates: lower case, whitespace collapsed to one space,
    punctuation stripped from both ends."""
    return " ".join(text.lower().split()).strip(string.punctuation + " ")


def universe_of(bundle: "Bundle") -> list[dict[str, Any]]:
    """The bundle's universe as [{symbol, name, sector}] (the benchmark is not part of it)."""
    return [{"symbol": s, "name": n, "sector": sector} for s, n, sector in bundle.members]


def tickers_in(text: str) -> set[str]:
    return {t for t in TICKER.findall(text) if t not in NOT_TICKERS}


def check_proposal(
    proposal: Proposal, *, generation: int, allowed: Iterable[str], parents: Iterable[str]
) -> str | None:
    """Why this proposal is refused, or None if it may be queued. The reason never quotes the
    proposal's text, so it is safe to count in logs."""
    name, instructions = proposal.name, proposal.instructions
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return "name"
    if not name.startswith(f"g{generation}-"):
        return "generation prefix"
    if not isinstance(instructions, str) or len(instructions.strip()) not in INSTRUCTIONS_LEN:
        return "length"
    if not tickers_in(instructions) <= set(allowed):
        return "ticker outside the universe"
    if not set(proposal.parents or ()) <= set(parents):
        return "unknown parent"
    return None

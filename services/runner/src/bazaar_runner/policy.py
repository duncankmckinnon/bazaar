"""The strategy-facing contract: what a decision policy receives and returns."""

from typing import Protocol

from bazaar_protocol import AccountSnapshot, ExperimentContext, OrderRequest, PriceObservation
from pydantic import AwareDatetime

Decision = tuple[OrderRequest, ...]


class PriceAt(Protocol):
    """As-of price lookup, bound to the market port and injected into a policy at construction.

    A cutoff later than the experiment's current trusted cutoff raises FutureData; a symbol with
    no close available by the cutoff raises MissingPrice (both in bazaar_runner.market).
    """

    async def __call__(self, symbol: str, cutoff: AwareDatetime) -> PriceObservation: ...


class DecisionPolicy(Protocol):
    """Called once per DECISION event; orders fill at ctx.simulated_at."""

    async def __call__(self, ctx: ExperimentContext, account: AccountSnapshot) -> Decision: ...

"""The house strategist (v8 F1): a performance brief from Logfire; propose() follows in S2.

brief() reads the scored runs of one window from Logfire with the read token in
$LOGFIRE_READ_TOKEN and ranks the strategies by mean return, with each one's excess over the
benchmark baseline, fills, LLM cost per run, adherence and budget-exhausted decisions. It is synchronous
and runs its own event loop (asyncio.run), so a worker thread can call it while runs execute.
Any failure raises StrategistError naming only the error's type: never the token or a response.
"""

import asyncio
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import logfire
from pydantic import BaseModel, ConfigDict

READ_TOKEN_ENV = "LOGFIRE_READ_TOKEN"
BENCHMARK_NAME = "baseline-buy-and-hold-spy"
ROW_LIMIT = 10_000
GENERATION = re.compile(r"^g(\d+)-")
# Tests replace this with an httpx.MockTransport; production uses the network.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

# Every Logfire query the strategist runs, in one place. Shapes verified against production
# records on 2026-10-12 (LOGFIRE-SHAPES.md): columns span_name, kind and attributes (JSON).
SCORES_SQL = """
SELECT
    attributes->>'bazaar.run_id' AS run_id,
    attributes->>'bazaar.strategy_name' AS strategy_name,
    attributes->>'bazaar.policy_kind' AS policy_kind,
    attributes->>'bazaar.data_version' AS data_version,
    attributes->>'return_pct' AS return_pct,
    attributes->>'fills' AS fills,
    attributes->>'decisions_exhausted' AS decisions_exhausted
FROM records
WHERE kind = 'log' AND span_name = 'bazaar.run scored'
"""
# operation.cost is a string on the model-request spans ('chat <model>').
COSTS_SQL = """
SELECT
    attributes->>'bazaar.run_id' AS run_id,
    SUM(CAST(attributes->>'operation.cost' AS DOUBLE)) AS cost_usd
FROM records
WHERE span_name LIKE 'chat %' AND attributes->>'operation.cost' IS NOT NULL
GROUP BY attributes->>'bazaar.run_id'
"""
# Optional: a run the judge never scored has no row, and its adherence is None.
ADHERENCE_SQL = """
SELECT
    attributes->>'bazaar.run_id' AS run_id,
    AVG(CAST(attributes->>'gen_ai.evaluation.score.value' AS DOUBLE)) AS adherence
FROM records
WHERE span_name = 'gen_ai.evaluation.result'
    AND attributes->>'gen_ai.evaluation.name' = 'strategy_adherence'
GROUP BY attributes->>'bazaar.run_id'
"""


class StrategistError(Exception):
    """The strategist could not do its job. The message never quotes a token or a response."""


class StrategyStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    runs: int
    return_pct: float  # mean over the strategy's runs
    excess_vs_benchmark_pct: float | None  # return_pct minus the benchmark's mean return
    fills: float
    llm_cost_usd: float | None  # mean per run with recorded model cost
    adherence: float | None  # mean strategy_adherence; None if never judged
    # Mean decisions per run that exhausted their budget (decisions_exhausted); every run has one
    # decision per session, so this is not a count of decisions.
    budget_exhausted: float
    policy_kind: str | None


class PerformanceBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    bundle_id: str
    since: datetime
    generated_at: datetime
    benchmark_return_pct: float | None
    runs_seen: int
    top: list[StrategyStats]
    bottom: list[StrategyStats]
    max_generation_seen: int | None

    @property
    def names(self) -> list[str]:
        """The top and bottom names: the only ones whose instructions propose() needs."""
        return [s.name for s in (*self.top, *self.bottom)]


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(result: Any) -> list[Mapping[str, Any]]:
    rows = result.get("rows") if isinstance(result, Mapping) else None
    if not isinstance(rows, list) or not all(isinstance(r, Mapping) for r in rows):
        raise StrategistError("Logfire returned an unexpected response shape")
    return rows


def _mean(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return statistics.fmean(known) if known else None


def aggregate(
    scores: list[Mapping[str, Any]],
    costs: list[Mapping[str, Any]],
    adherence: list[Mapping[str, Any]],
    *,
    bundle_id: str,
    since: datetime,
    generated_at: datetime,
    top: int = 10,
    bottom: int = 5,
    exclude_names: frozenset[str] = frozenset(),
    data_version: str | None = None,
) -> PerformanceBrief:
    """Rank the strategies of one window from the three queries' rows (pure; no Logfire)."""
    cost_by_run = {r.get("run_id"): _number(r.get("cost_usd")) for r in costs}
    adherence_by_run = {r.get("run_id"): _number(r.get("adherence")) for r in adherence}
    runs: dict[str, Mapping[str, Any]] = {}
    for row in scores:
        run_id, name = row.get("run_id"), row.get("strategy_name")
        if not run_id or not name or _number(row.get("return_pct")) is None:
            continue
        # Runs scored before bazaar.data_version existed have none: kept, bounded by `since`.
        if data_version is not None and row.get("data_version") not in (None, "", data_version):
            continue
        # Hidden or moderated submissions and operator smoke rows never reach the brief.
        if name in exclude_names:
            continue
        runs.setdefault(run_id, row)

    benchmark = _mean(
        _number(r.get("return_pct")) for r in runs.values() if r["strategy_name"] == BENCHMARK_NAME
    )
    by_name: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for run_id, row in runs.items():
        if row.get("policy_kind") == "baseline" or row["strategy_name"] == BENCHMARK_NAME:
            continue  # the benchmark is reported on its own; baselines are never ranked
        by_name[row["strategy_name"]].append((run_id, row))

    stats = []
    for name, group in by_name.items():
        mean_return = _mean(_number(r.get("return_pct")) for _, r in group)
        kinds = Counter(r.get("policy_kind") for _, r in group).most_common(1)
        stats.append(
            StrategyStats(
                name=name,
                runs=len(group),
                return_pct=mean_return,
                excess_vs_benchmark_pct=None if benchmark is None else mean_return - benchmark,
                fills=_mean(_number(r.get("fills")) for _, r in group) or 0.0,
                llm_cost_usd=_mean(cost_by_run.get(run_id) for run_id, _ in group),
                adherence=_mean(adherence_by_run.get(run_id) for run_id, _ in group),
                budget_exhausted=_mean(_number(r.get("decisions_exhausted")) for _, r in group)
                or 0.0,
                policy_kind=kinds[0][0] if kinds else None,
            )
        )
    ranked = sorted(stats, key=lambda s: (-s.return_pct, s.name))
    best = ranked[:top]
    rest = [s for s in reversed(ranked) if s not in best]
    generations = [
        int(m.group(1)) for r in runs.values() if (m := GENERATION.match(r["strategy_name"]))
    ]
    return PerformanceBrief(
        bundle_id=bundle_id,
        since=since,
        generated_at=generated_at,
        benchmark_return_pct=benchmark,
        runs_seen=len(runs),
        top=best,
        bottom=rest[:bottom],
        max_generation_seen=max(generations) if generations else None,
    )


async def _query(since: datetime, environment: str, token: str) -> tuple[list, list, list]:
    from logfire.query_client import AsyncLogfireQueryClient

    kwargs = {"transport": _TRANSPORT} if _TRANSPORT is not None else {}
    async with AsyncLogfireQueryClient(read_token=token, **kwargs) as client:
        results = []
        for sql in (SCORES_SQL, COSTS_SQL, ADHERENCE_SQL):
            result = await client.query_json_rows(
                sql, min_timestamp=since, environment=environment, limit=ROW_LIMIT
            )
            results.append(_rows(result))
    return results[0], results[1], results[2]


def brief(
    *,
    bundle_id: str,
    since: datetime,
    environment: str = "production",
    top: int = 10,
    bottom: int = 5,
    exclude_names: frozenset[str] = frozenset(),
    data_version: str | None = None,
) -> PerformanceBrief:
    """The window's performance brief: runs scored at or after `since` in `environment`.

    data_version, when given, keeps runs whose bazaar.data_version equals it or is absent.
    exclude_names are removed before any aggregate or rank. No read token is an error, never
    an empty brief."""
    token = os.environ.get(READ_TOKEN_ENV)
    if not token:
        raise StrategistError(f"${READ_TOKEN_ENV} is not set")
    with logfire.span("strategist.brief", bundle_id=bundle_id, environment=environment):
        failure: StrategistError | None = None
        try:
            scores, costs, adherence = asyncio.run(_query(since, environment, token))
        except StrategistError as exc:
            failure = StrategistError(str(exc))
        except Exception as exc:  # noqa: BLE001 - only the type: details could quote the token
            failure = StrategistError(f"the Logfire query failed ({type(exc).__name__})")
        if failure is not None:
            # Raised outside the except block, so the original error (whose text can quote the
            # token or a response) is not even its context.
            raise failure
        return aggregate(
            scores,
            costs,
            adherence,
            bundle_id=bundle_id,
            since=since,
            generated_at=datetime.now(UTC),
            top=top,
            bottom=bottom,
            exclude_names=frozenset(exclude_names),
            data_version=data_version,
        )

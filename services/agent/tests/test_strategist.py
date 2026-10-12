"""brief(): a window's performance from Logfire, offline. Invented strategies and numbers only.

The fake Logfire holds records with the production attribute names (LOGFIRE-SHAPES.md,
2026-10-12) and answers each /v2/query by projecting them the way the SQL selects them."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from bazaar_agent import strategist
from bazaar_agent.strategist import BENCHMARK_NAME, StrategistError, brief

TOKEN = "pylf_v1_us_readtokendonotleak0123456789"
SINCE = datetime(2026, 10, 11, 20, 14, tzinfo=UTC)


def scored(run_id, name, return_pct, *, kind="agent", fills=2, exhausted=0, bundle=None):
    attributes = {
        "bazaar.run_id": run_id,
        "bazaar.strategy_name": name,
        "bazaar.policy_kind": kind,
        "bazaar.experiment_id": f"exp-{run_id}",
        "return_pct": return_pct,
        "fills": fills,
        "ending_value": 10000 * (1 + return_pct / 100),
        "decisions_exhausted": exhausted,
    }
    if bundle is not None:
        attributes["bazaar.data_version"] = bundle
    return {"kind": "log", "span_name": "bazaar.run scored", "attributes": attributes}


def chat(run_id, cost):
    return {
        "kind": "span",
        "span_name": "chat gpt-5.6-sol",
        "attributes": {"bazaar.run_id": run_id, "operation.cost": str(cost)},
    }


def judged(run_id, score, name="strategy_adherence"):
    return {
        "kind": "log",
        "span_name": "gen_ai.evaluation.result",
        "attributes": {
            "bazaar.run_id": run_id,
            "gen_ai.evaluation.name": name,
            "gen_ai.evaluation.score.value": score,
        },
    }


RECORDS = [
    scored("a1", "momo-a", 2.0, fills=2, exhausted=1),
    scored("a2", "momo-a", 4.0, fills=4, exhausted=0),
    scored("b1", "news-b", 5.0, fills=1),
    scored("c1", "value-c", -1.0, fills=0),
    scored("s1", BENCHMARK_NAME, 1.0, kind="baseline", fills=1),
    scored("s2", BENCHMARK_NAME, 3.0, kind="baseline", fills=1),
    scored("k1", "baseline-cash-only", 0.0, kind="baseline", fills=0),
    chat("a1", 0.01),
    chat("a1", 0.02),
    chat("a2", 0.05),
    chat("b1", 0.04),
    judged("a1", 1.0),
    judged("a1", 0.5),
    judged("a1", 0.0, name="strategy_adherence_pass"),  # a different evaluation, never averaged
]


class FakeLogfire:
    def __init__(self, records=RECORDS, status=200, body=None):
        self.records, self.status, self.body = records, status, body
        self.requests: list[httpx.Request] = []

    def project(self, sql: str) -> list[dict]:
        if "bazaar.run scored" in sql:
            return [
                {
                    "run_id": r["attributes"]["bazaar.run_id"],
                    "strategy_name": r["attributes"]["bazaar.strategy_name"],
                    "policy_kind": r["attributes"]["bazaar.policy_kind"],
                    "data_version": r["attributes"].get("bazaar.data_version"),
                    "return_pct": str(r["attributes"]["return_pct"]),
                    "fills": str(r["attributes"]["fills"]),
                    "decisions_exhausted": str(r["attributes"]["decisions_exhausted"]),
                }
                for r in self.records
                if r["kind"] == "log" and r["span_name"] == "bazaar.run scored"
            ]
        totals: dict[str, list[float]] = {}
        if "operation.cost" in sql:
            for r in self.records:
                if r["span_name"].startswith("chat ") and "operation.cost" in r["attributes"]:
                    totals.setdefault(r["attributes"]["bazaar.run_id"], []).append(
                        float(r["attributes"]["operation.cost"])
                    )
            return [{"run_id": k, "cost_usd": sum(v)} for k, v in totals.items()]
        for r in self.records:
            a = r["attributes"]
            if (
                r["span_name"] == "gen_ai.evaluation.result"
                and a["gen_ai.evaluation.name"] == "strategy_adherence"
            ):
                totals.setdefault(a["bazaar.run_id"], []).append(a["gen_ai.evaluation.score.value"])
        return [{"run_id": k, "adherence": sum(v) / len(v)} for k, v in totals.items()]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text=f"denied for token {TOKEN}")
        if self.body is not None:
            return httpx.Response(200, json=self.body)
        sql = json.loads(request.content)["sql"]
        # The /v2/query wire shape; the client maps it to {"columns", "rows"}.
        return httpx.Response(200, json={"schema": {"fields": []}, "data": self.project(sql)})


@pytest.fixture
def logfire_api(monkeypatch):
    monkeypatch.setenv(strategist.READ_TOKEN_ENV, TOKEN)

    def install(fake: FakeLogfire) -> FakeLogfire:
        monkeypatch.setattr(strategist, "_TRANSPORT", httpx.MockTransport(fake))
        return fake

    return install


def by_name(stats):
    return {s.name: s for s in stats}


def test_each_query_posts_to_v2_with_the_read_token_and_window(logfire_api):
    fake = logfire_api(FakeLogfire())
    brief(bundle_id="demo-bundle-v2", since=SINCE)

    assert len(fake.requests) == 3
    for request in fake.requests:
        assert request.method == "POST" and request.url.path == "/v2/query"
        assert request.headers["authorization"] == TOKEN
        body = json.loads(request.content)
        assert body["min_timestamp"] == SINCE.isoformat()
        assert "production" in json.dumps(body.get("environment", body))
    sqls = [json.loads(r.content)["sql"] for r in fake.requests]
    assert "span_name = 'bazaar.run scored'" in sqls[0]
    assert "'operation.cost'" in sqls[1] and "span_name LIKE 'chat %'" in sqls[1]
    assert "'gen_ai.evaluation.score.value'" in sqls[2] and "'strategy_adherence'" in sqls[2]


def test_strategies_are_ranked_with_benchmark_excess_cost_and_adherence(logfire_api):
    logfire_api(FakeLogfire())
    result = brief(bundle_id="demo-bundle-v2", since=SINCE, top=2, bottom=1)

    # The benchmark is the mean of the SPY buy-and-hold runs: (1 + 3) / 2.
    assert result.benchmark_return_pct == 2.0
    assert [s.name for s in result.top] == ["news-b", "momo-a"]
    assert [s.name for s in result.bottom] == ["value-c"]
    assert result.names == ["news-b", "momo-a", "value-c"]
    stats = by_name(result.top + result.bottom)
    momo = stats["momo-a"]
    assert (momo.runs, momo.return_pct, momo.excess_vs_benchmark_pct) == (2, 3.0, 1.0)
    assert momo.fills == 3.0 and momo.budget_exhausted == 0.5
    assert "decisions" not in type(momo).model_fields
    assert momo.llm_cost_usd == pytest.approx((0.03 + 0.05) / 2)
    # Only a1 was judged (mean of 1.0 and 0.5); a2 never was, so it doesn't count as 0.
    assert momo.adherence == pytest.approx(0.75)
    assert stats["news-b"].adherence is None and stats["news-b"].excess_vs_benchmark_pct == 3.0
    assert stats["value-c"].llm_cost_usd is None
    # Baselines are never ranked; every scored run in the window was seen.
    assert not {BENCHMARK_NAME, "baseline-cash-only"} & set(result.names)
    assert result.runs_seen == 7 and result.max_generation_seen is None


def test_excluded_names_are_gone_before_ranking(logfire_api):
    logfire_api(FakeLogfire())
    result = brief(
        bundle_id="demo-bundle-v2",
        since=SINCE,
        top=2,
        bottom=1,
        exclude_names=frozenset({"news-b"}),
    )
    assert [s.name for s in result.top] == ["momo-a", "value-c"]
    assert result.bottom == [] and "news-b" not in result.names
    assert result.runs_seen == 6


def test_data_version_keeps_the_bundle_and_runs_without_one(logfire_api):
    records = [
        scored("x1", "this-bundle", 1.0, bundle="demo-bundle-v2"),
        scored("x2", "no-bundle", 2.0),
        scored("x3", "old-bundle", 9.0, bundle="demo-bundle-v1"),
    ]
    logfire_api(FakeLogfire(records))
    kept = brief(bundle_id="demo-bundle-v2", since=SINCE, data_version="demo-bundle-v2")
    assert {s.name for s in kept.top} == {"this-bundle", "no-bundle"}
    everything = brief(bundle_id="demo-bundle-v2", since=SINCE)
    assert {s.name for s in everything.top} == {"this-bundle", "no-bundle", "old-bundle"}


def test_no_benchmark_means_no_excess(logfire_api):
    logfire_api(FakeLogfire([scored("a1", "momo-a", 2.0)]))
    result = brief(bundle_id="demo-bundle-v2", since=SINCE)
    assert result.benchmark_return_pct is None and result.top[0].excess_vs_benchmark_pct is None


def test_the_highest_generation_is_read_from_house_names(logfire_api):
    records = [scored("g1", "g3-trend", 1.0), scored("g2", "g12-mean-revert", 2.0)]
    logfire_api(FakeLogfire(records))
    assert brief(bundle_id="demo-bundle-v2", since=SINCE).max_generation_seen == 12


def test_no_read_token_is_an_error_not_an_empty_brief(monkeypatch):
    monkeypatch.delenv(strategist.READ_TOKEN_ENV, raising=False)
    fake = FakeLogfire()
    monkeypatch.setattr(strategist, "_TRANSPORT", httpx.MockTransport(fake))
    with pytest.raises(StrategistError, match="LOGFIRE_READ_TOKEN is not set"):
        brief(bundle_id="demo-bundle-v2", since=SINCE)
    assert fake.requests == []


@pytest.mark.parametrize(
    "fake",
    [
        FakeLogfire(status=401),
        FakeLogfire(status=500),
        FakeLogfire(body={"schema": {"fields": []}, "data": "not a list"}),
        FakeLogfire(body={"columns": [], "rows": []}),  # not the /v2 shape
        FakeLogfire(body=["not", "an", "object"]),
    ],
)
def test_logfire_failures_raise_without_the_token(logfire_api, fake):
    logfire_api(fake)
    with pytest.raises(StrategistError) as caught:
        brief(bundle_id="demo-bundle-v2", since=SINCE)
    assert TOKEN not in str(caught.value)
    assert (
        caught.value.__cause__ is None
        and caught.value.__context__ is None
        or (TOKEN not in repr(caught.value.__context__))
    )


def test_brief_is_safe_from_worker_threads(logfire_api):
    logfire_api(FakeLogfire())
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: brief(bundle_id="demo-bundle-v2", since=SINCE), range(3)))
    assert {tuple(r.names) for r in results} == {("news-b", "momo-a", "value-c")}

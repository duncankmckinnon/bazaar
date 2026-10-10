import importlib.util
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).parents[1] / "loadtest.py"
spec = importlib.util.spec_from_file_location("loadtest", SCRIPT)
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)

STAGING = "https://pydantic-agents--bazaar-staging-staging-web.modal.run"
TOKEN = "SENTINEL-ADMIN-TOKEN"
STATS = {
    "window": {"since": "2026-10-10T15:00:00+00:00"},
    "submissions": [],
    "aggregates": {
        "count_by_status": {"scored": 5, "failed": 1},
        "runs_per_hour": 12.345,
        "run_s": {"p50": 240.0, "p95": 410.5},
        "queue_wait_s": {"p50": 3.0, "p95": 95.25},
        "failures_by_class": {"TimeoutError": 1},
        "in_flight": 0,
        "max_concurrent": 3,
        "sqlite_locked_errors": 2,
    },
}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds


class Staging:
    """A fake web service: each submission is queued, then running, then its final status."""

    def __init__(self, fail=(), refuse=(), polls_until_done=2):
        self.fail, self.refuse, self.polls_until_done = set(fail), set(refuse), polls_until_done
        self.posts, self.polls, self.requests = [], {}, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/api/submissions":
            body = json.loads(request.content)
            self.posts.append(body)
            if body["name"].split("-")[2] in self.refuse:
                return httpx.Response(429, json={"detail": "Too many submissions"})
            return httpx.Response(201, json={"id": f"id-{len(self.posts) - 1}"})
        if request.method == "GET" and path.startswith("/api/submissions/"):
            sid = path.rsplit("/", 1)[1]
            n = self.polls[sid] = self.polls.get(sid, 0) + 1
            index = sid.removeprefix("id-")
            status = "running" if n < self.polls_until_done else (
                "failed" if index in self.fail else "scored"
            )  # fmt: skip
            return httpx.Response(200, json={"id": sid, "status": status})
        if request.method == "GET" and path == "/api/admin/stats":
            assert request.headers["X-Bazaar-Admin-Token"] == TOKEN
            return httpx.Response(200, json=STATS)
        return httpx.Response(404)


def real_mix(tmp_path):
    path = tmp_path / "mix.json"
    labels = ["smoke-1", "smoke-2", "smoke-3", "ko-hype", "momentum-hype", "trump-trading"]
    path.write_text(
        json.dumps([{"label": x, "instructions": f"{x} strategy text"} for x in labels])
    )
    return path


def go(tmp_path, server, *extra, env=None, url=STAGING):
    out, clock = io.StringIO(), Clock()
    client = httpx.Client(base_url=url, transport=httpx.MockTransport(server))
    code = loadtest.main(
        ["--url", url, "--level", "3", "--mix", str(real_mix(tmp_path)),
         "--out", str(tmp_path / "results"), *extra],
        client=client,
        env={"BAZAAR_ADMIN_TOKEN": TOKEN} if env is None else env,
        out=out,
        sleep=clock.sleep,
        clock=clock.now,
        utcnow=lambda: datetime(2026, 10, 10, 15, 0, tzinfo=UTC),
    )  # fmt: skip
    return code, out.getvalue(), clock


# ---------- the production guard ----------


@pytest.mark.parametrize(
    "url",
    [
        "https://pydantic-agents--bazaar-live-live-web.modal.run",
        "https://pydantic-agents--bazaar-live-live-web.modal.run/",
        "https://PYDANTIC-AGENTS--BAZAAR-LIVE-LIVE-WEB.MODAL.RUN",
        "https://pydantic-agents--bazaar-live-live-web.modal.run.",
        "https://staging@pydantic-agents--bazaar-live-live-web.modal.run",
        "https://pydantic-agents--bazaar-live-live-web.modal.run/staging",
        "https://pydantic-agents--bazaar-live-live-web.modal.run?staging=1",
        "https://staging-pydantic-agents--bazaar-live-live-web.modal.run",
    ],
)
def test_production_is_refused_even_with_go(tmp_path, url):
    server = Staging()
    code, out, _ = go(tmp_path, server, "--go", url=url)

    assert code == 2
    assert out.startswith("REFUSED:")
    assert server.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "https://bazaar.example",
        "https://pydantic-agents--bazaar-web.modal.run",
        "http://pydantic-agents--bazaar-staging-staging-web.modal.run",  # not https
        "https://user:pw@bazaar-staging.example",
        "pydantic-agents--bazaar-staging-staging-web.modal.run",  # no scheme
    ],
)
def test_a_host_that_is_not_https_staging_is_refused(tmp_path, url):
    server = Staging()
    code, out, _ = go(tmp_path, server, "--go", url=url)

    assert (code, out.split(":")[0], server.requests) == (2, "REFUSED", [])


def test_the_production_host_is_a_hard_coded_deny_entry():
    assert "pydantic-agents--bazaar-live-live-web.modal.run" in loadtest.DENIED_HOSTS
    with pytest.raises(loadtest.Refused, match="production"):
        loadtest.staging_base("https://pydantic-agents--bazaar-live-live-web.modal.run")


# ---------- dry run and caps ----------


def test_the_default_is_a_dry_run_that_posts_nothing(tmp_path):
    server = Staging()
    code, out, _ = go(tmp_path, server, "--cost-per-run", "0.75")

    assert code == 0
    assert server.requests == []
    assert "dry run: nothing posted" in out
    assert "runs: 6" in out and "level: 3" in out
    assert "estimated model cost: $4.50 (6 x $0.75)" in out
    assert "ko-hype, momentum-hype, trump-trading" in out
    assert not (tmp_path / "results").exists()


def test_runs_above_max_runs_are_refused(tmp_path):
    server = Staging()
    assert go(tmp_path, server, "--runs", "41", "--go")[0] == 2
    assert go(tmp_path, server, "--runs", "5", "--max-runs", "4", "--go")[0] == 2
    assert go(tmp_path, server, "--runs", "0", "--go")[0] == 2
    assert server.requests == []


def test_go_needs_the_token_from_the_env_and_no_cli_flag_takes_it(tmp_path):
    server = Staging()
    code, out, _ = go(tmp_path, server, "--go", env={})

    assert code == 2 and "BAZAAR_ADMIN_TOKEN is not set" in out
    assert server.requests == []
    with pytest.raises(SystemExit):
        loadtest.parser().parse_args(["--url", STAGING, "--level", "3", "--token", "x"])


def test_placeholder_strategies_never_run(tmp_path):
    server = Staging()
    out, clock = io.StringIO(), Clock()
    code = loadtest.main(
        ["--url", STAGING, "--level", "1", "--go"],  # the committed mix file
        client=httpx.Client(base_url=STAGING, transport=httpx.MockTransport(server)),
        env={"BAZAAR_ADMIN_TOKEN": TOKEN},
        out=out,
        sleep=clock.sleep,
        clock=clock.now,
    )

    assert code == 2 and "placeholder" in out.getvalue()
    assert server.requests == []
    mix = json.loads(loadtest.DEFAULT_MIX.read_text())
    assert [m["label"] for m in mix] == [
        "smoke-1", "smoke-2", "smoke-3", "ko-hype", "momentum-hype", "trump-trading",
    ]  # fmt: skip


# ---------- --go ----------


def test_go_posts_2n_unique_names_with_the_mix_rotated_and_polls_to_the_end(tmp_path):
    server = Staging(polls_until_done=3)
    code, _, clock = go(tmp_path, server, "--go")

    assert code == 0
    names = [p["name"] for p in server.posts]
    assert len(names) == 6 and len(set(names)) == 6
    assert all(loadtest.SLUG.match(n) and n.startswith(f"lt-3-{i}-") for i, n in enumerate(names))
    assert [p["instructions"] for p in server.posts] == [
        f"{x} strategy text"
        for x in ["smoke-1", "smoke-2", "smoke-3", "ko-hype", "momentum-hype", "trump-trading"]
    ]
    assert all(n == 3 for n in server.polls.values())
    assert clock.sleeps == 2  # 5 s between rounds; none after the last
    stats_request = server.requests[-1]
    assert stats_request.url.path == "/api/admin/stats"
    assert stats_request.url.params["since"] == "2026-10-10T15:00:00+00:00"


def test_the_results_table_and_json_from_a_canned_stats_response(tmp_path):
    code, out, _ = go(tmp_path, Staging(fail={"4"}), "--go")

    assert code == 1  # one run failed
    md = (tmp_path / "results" / "3-20261010T150000Z.md").read_text()
    header, _, row = md.strip().splitlines()
    assert header.startswith("| level | runs | scored | failed | runs/hour |")
    assert "decision latency (from Logfire)" in header and "CPU/mem (from Modal)" in header
    assert row == (
        "| 3 | 6 | 5 | 1 | 12.3 | 240.0 | 410.5 | 3.0 | 95.2 | TimeoutError 1 | 2 "
        "| n/a | n/a | n/a |"
    )
    assert row in out
    result = json.loads((tmp_path / "results" / "3-20261010T150000Z.json").read_text())
    assert result["stats"] == STATS
    assert [r["status"] for r in result["runs"]].count("failed") == 1


def test_a_refused_post_and_a_timeout_both_fail_the_run(tmp_path):
    code, out, _ = go(tmp_path, Staging(refuse={"1"}), "--go")
    assert code == 1 and "refused (429: Too many submissions)" in out

    code, out, clock = go(tmp_path, Staging(polls_until_done=10**6), "--go", "--timeout-min", "1")
    assert code == 1 and "still not done after 1.0 min" in out
    assert clock.t >= 60


def test_the_admin_token_never_reaches_stdout_or_the_results(tmp_path):
    code, out, _ = go(tmp_path, Staging(fail={"0"}, refuse={"2"}), "--go")
    assert code == 1

    assert TOKEN not in out
    for path in (tmp_path / "results").iterdir():
        assert TOKEN not in path.read_text()


def test_redirects_are_not_followed(tmp_path):
    def redirect(request):
        if request.url.host != "pydantic-agents--bazaar-staging-staging-web.modal.run":
            raise AssertionError(f"left staging for {request.url.host}")
        return httpx.Response(
            307, headers={"Location": f"https://{loadtest.PROD_HOST}{request.url.path}"}
        )

    out = io.StringIO()
    client = httpx.Client(base_url=STAGING, transport=httpx.MockTransport(redirect))
    assert client.follow_redirects is False  # httpx's default, which the script also sets
    code = loadtest.main(
        ["--url", STAGING, "--level", "1", "--mix", str(real_mix(tmp_path)), "--go",
         "--out", str(tmp_path / "r")],
        client=client, env={"BAZAAR_ADMIN_TOKEN": TOKEN}, out=out,
        sleep=lambda s: None, clock=lambda: 0.0,
    )  # fmt: skip

    assert code == 1


def test_the_real_client_stays_on_the_staging_host():
    with loadtest.staging_client(loadtest.staging_base(STAGING + "/x?y=1")) as client:
        assert client.follow_redirects is False
        assert str(client.base_url).rstrip("/") == STAGING  # path and query dropped

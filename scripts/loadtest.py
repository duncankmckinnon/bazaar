"""Load test for a STAGING web deploy: run N strategies at once and record the throughput.

    uv run python scripts/loadtest.py --url https://<staging-host> --level N
        [--runs 2N] [--mix scripts/loadtest_mix.json] [--cost-per-run USD] [--go] [--out results/]

Never production. The URL's host must contain "staging" and must not be the production host,
and redirects are never followed. Without --go it only prints the plan and exits; nothing is
posted. With --go it posts --runs submissions (default 2 x level, at most --max-runs) with the
mix's instructions in rotation, polls each one every 5 s until it is scored or failed (or
--timeout-min passes), then reads /api/admin/stats for the test window and writes
<out>/<level>-<utc>.json and .md. The admin token comes only from the BAZAAR_ADMIN_TOKEN
environment variable and is never printed. Exit codes: 0 every run scored (or dry run), 1 a run
failed, was refused or timed out, 2 refused or bad usage.
"""

import argparse
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx

PROD_HOST = "pydantic-agents--bazaar-live-live-web.modal.run"
DENIED_HOSTS = frozenset({PROD_HOST})
DEFAULT_MIX = Path(__file__).with_name("loadtest_mix.json")
PLACEHOLDER = "PLACEHOLDER"
POLL_S = 5.0
SLUG = re.compile(r"^[a-z0-9-]{3,40}$")
TERMINAL = ("scored", "failed")
COLUMNS = [
    "level", "runs", "scored", "failed", "runs/hour", "run_s p50", "run_s p95",
    "queue_wait p50", "queue_wait p95", "failures_by_class", "sqlite_lock_retries",
    "decision latency (from Logfire)", "Gateway 429s (from Logfire)", "CPU/mem (from Modal)",
]  # fmt: skip


class Refused(Exception):
    """The run must not start; the message says why."""


def staging_base(url: str) -> str:
    """The base URL when it is safe to load: https, a staging host, never production."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https":
        raise Refused(f"refusing {url!r}: the URL must be https://")
    if host in DENIED_HOSTS or "bazaar-live" in host:
        raise Refused(f"refusing {host}: that is the production host")
    if "staging" not in host:
        raise Refused(f"refusing {host}: the host must contain 'staging'")
    if parts.username or parts.password:
        raise Refused("refusing a URL with credentials in it")
    return f"https://{parts.netloc}"


def load_mix(path: Path) -> list[dict[str, str]]:
    mix = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(mix, list) or not mix:
        raise Refused(f"{path}: expected a non-empty list of strategies")
    for entry in mix:
        if not isinstance(entry, dict) or not {"label", "instructions"} <= entry.keys():
            raise Refused(f"{path}: each strategy needs a label and instructions")
    return mix


def run_names(level: int, runs: int) -> list[str]:
    names = [f"lt-{level}-{i}-{secrets.token_hex(2)}" for i in range(runs)]
    assert all(SLUG.match(n) for n in names) and len(set(names)) == runs, names
    return names


def plan_text(
    base: str, level: int, names: list[str], mix: list[dict[str, str]], cost: float | None
) -> str:
    lines = [
        f"target: {base}",
        f"level: {level} concurrent (the staging deploy must run BAZAAR_MAX_CONCURRENT={level})",
        f"runs: {len(names)}",
        "strategy mix (rotated): " + ", ".join(m["label"] for m in mix),
    ]
    for i, name in enumerate(names):
        lines.append(f"  {name}  {mix[i % len(mix)]['label']}")
    if cost is not None:
        lines.append(f"estimated model cost: ${cost * len(names):.2f} ({len(names)} x ${cost:.2f})")
    return "\n".join(lines)


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in sorted(value.items())) or "none"
    return str(value)


def table(level: int, runs: int, finals: dict[str, str], stats: dict[str, Any] | None) -> str:
    agg = (stats or {}).get("aggregates", {})
    run_s, wait = agg.get("run_s") or {}, agg.get("queue_wait_s") or {}
    statuses = list(finals.values())
    row = [
        level, runs, statuses.count("scored"), statuses.count("failed"), agg.get("runs_per_hour"),
        run_s.get("p50"), run_s.get("p95"), wait.get("p50"), wait.get("p95"),
        agg.get("failures_by_class"), agg.get("sqlite_lock_retries"), None, None, None,
    ]  # fmt: skip
    return "\n".join(
        [
            "| " + " | ".join(COLUMNS) + " |",
            "|" + "---|" * len(COLUMNS),
            "| " + " | ".join(fmt(v) for v in row) + " |",
        ]
    )


def detail(response: httpx.Response) -> str:
    try:
        text = response.json().get("detail")
    except (ValueError, AttributeError):
        text = None
    return " ".join(str(text or response.text).split())[:200]


def run(
    args: argparse.Namespace,
    *,
    client: httpx.Client,
    token: str,
    out: TextIO,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    names: list[str],
    mix: list[dict[str, str]],
) -> int:
    started = utcnow()
    finals: dict[str, str] = {}
    ids: dict[str, str] = {}
    for i, name in enumerate(names):
        body = {"name": name, "handle": None, "instructions": mix[i % len(mix)]["instructions"]}
        response = client.post("/api/submissions", json=body)
        if response.status_code != 201:
            print(f"{name}: refused ({response.status_code}: {detail(response)})", file=out)
            finals[name] = "refused"
            continue
        ids[name] = response.json()["id"]
    print(f"posted {len(ids)} of {len(names)}", file=out)

    deadline = clock() + args.timeout_min * 60
    pending = dict(ids)
    while pending and clock() < deadline:
        for name, submission_id in list(pending.items()):
            response = client.get(f"/api/submissions/{submission_id}")
            status = response.json().get("status") if response.status_code == 200 else None
            if status in TERMINAL:
                finals[name] = status
                del pending[name]
                print(f"{name}: {status}", file=out)
        if pending:
            sleep(POLL_S)
    for name in pending:
        finals[name] = "timeout"
        print(f"{name}: still not done after {args.timeout_min} min", file=out)

    response = client.get(
        "/api/admin/stats",
        params={"since": started.isoformat()},
        headers={"X-Bazaar-Admin-Token": token},
    )
    stats = response.json() if response.status_code == 200 else None
    if stats is None:
        print(f"stats unavailable ({response.status_code})", file=out)

    markdown = table(args.level, len(names), finals, stats)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"{args.level}-{started:%Y%m%dT%H%M%SZ}"
    result = {
        "level": args.level,
        "started_at": started.isoformat(),
        "runs": [{"name": n, "id": ids.get(n), "status": finals[n]} for n in names],
        "stats": stats,
    }
    stem.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown, file=out)
    print(f"wrote {stem}.json and {stem}.md", file=out)
    return 0 if all(s == "scored" for s in finals.values()) else 1


def staging_client(base: str) -> httpx.Client:
    # follow_redirects=False: a redirect can never carry the run to another host.
    return httpx.Client(base_url=base, timeout=30, follow_redirects=False)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", required=True, help="the STAGING web base URL (https)")
    p.add_argument("--level", type=int, required=True, help="BAZAAR_MAX_CONCURRENT under test")
    p.add_argument("--runs", type=int, help="submissions to post (default 2 x level)")
    p.add_argument("--max-runs", type=int, default=40, help="refuse more runs than this")
    p.add_argument("--mix", type=Path, default=DEFAULT_MIX)
    p.add_argument("--cost-per-run", type=float, help="USD, for the plan's estimate")
    p.add_argument("--timeout-min", type=float, default=45.0)
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--go", action="store_true", help="actually post; without it, a dry run")
    return p


def main(
    argv: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    env: dict[str, str] | None = None,
    out: TextIO = sys.stdout,
    **clocks: Any,
) -> int:
    args = parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        base = staging_base(args.url)
        if not 1 <= args.level <= 32:
            raise Refused("--level must be 1 to 32 (BAZAAR_MAX_CONCURRENT's range)")
        runs = 2 * args.level if args.runs is None else args.runs
        if not 1 <= runs <= args.max_runs:
            raise Refused(f"refusing {runs} runs: must be 1 to --max-runs ({args.max_runs})")
        mix = load_mix(args.mix)
        names = run_names(args.level, runs)
        print(plan_text(base, args.level, names, mix, args.cost_per_run), file=out)
        if not args.go:
            print("dry run: nothing posted. Add --go to run it.", file=out)
            return 0
        if any(m["instructions"].startswith(PLACEHOLDER) for m in mix):
            raise Refused(f"{args.mix} still has placeholder strategies; fill them in first")
        token = env.get("BAZAAR_ADMIN_TOKEN")
        if not token:
            raise Refused("BAZAAR_ADMIN_TOKEN is not set (needed for /api/admin/stats)")
    except Refused as exc:
        print(f"REFUSED: {exc}", file=out)
        return 2

    with client or staging_client(base) as http:
        return run(args, client=http, token=token, out=out, names=names, mix=mix, **clocks)


if __name__ == "__main__":
    sys.exit(main())

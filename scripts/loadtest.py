"""Load test for a STAGING web deploy: run N strategies at once and record the throughput.

    uv run python scripts/loadtest.py --url https://<staging-host> --level N
        [--runs 2N] [--mix-from-house-feeder scripts/house_feeder.py | --mix MIX.json]
        [--admin-token-file PATH] [--cost-per-run USD] [--go] [--out results/]

Never production. The URL's host must contain "staging" and must not be the production host,
and redirects are never followed. Without --go it only prints the plan and exits; nothing is
posted. With --go it posts --runs submissions (default 2 x level, at most --max-runs) with the
mix's instructions in rotation, polls each one every 5 s until it is scored or failed (or
--timeout-min passes), then reads /api/admin/stats for the test window and writes
<out>/<level>-<utc>.json and .md with the window's started_at and finished_at.

The mix is the house feeder's strategies() (instructions only; its own posting functions are
never called) or a JSON list of {label, instructions}. The admin token comes from
--admin-token-file (a file only its owner can read) or else BAZAAR_ADMIN_TOKEN; it is never
printed. Exit codes: 0 every run scored (or dry run), 1 a run failed, was refused or timed
out, 2 refused or bad usage.
"""

import argparse
import importlib.util
import itertools
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
    "level", "started_at", "finished_at", "runs", "scored", "failed", "runs/hour", "run_s p50", "run_s p95",
    "queue_wait p50", "queue_wait p95", "failures_by_class", "sqlite_locked_errors",
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


def load_feeder_mix(path: Path, count: int) -> list[dict[str, str]]:
    """The first count strategies of a house feeder file. Only strategies() is called."""
    if not path.is_file():
        raise Refused(f"{path}: no such house feeder file")
    spec = importlib.util.spec_from_file_location("loadtest_house_feeder", path)
    if spec is None or spec.loader is None:
        raise Refused(f"{path}: cannot load it as a Python module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        pairs = list(itertools.islice(module.strategies(), count))
    except Exception as exc:  # noqa: BLE001 - any import or generator failure stops the run
        raise Refused(f"{path}: strategies() failed ({type(exc).__name__}: {exc})") from None
    mix = []
    for pair in pairs:
        if not (
            isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(x, str) for x in pair)
        ):
            raise Refused(f"{path}: strategies() must yield (name, instructions) string pairs")
        name, instructions = pair  # our lt- names are used; the feeder's name only labels it
        mix.append({"label": f"house:{name}", "instructions": instructions})
    if not mix:
        raise Refused(f"{path}: strategies() yielded nothing")
    return mix


def read_token_file(path: Path) -> str:
    """The stripped token. Refused unless only its owner can read it. Never echoes contents."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise Refused(f"{path}: cannot read the admin token file ({type(exc).__name__})") from None
    if mode & 0o077:
        raise Refused(
            f"{path}: the admin token file is readable by group or others "
            f"(mode {mode & 0o777:o}); chmod 600 it"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"{path}: cannot read the admin token file ({type(exc).__name__})") from None
    if not token:
        raise Refused(f"{path}: the admin token file is empty")
    return token


def run_names(level: int, runs: int) -> list[str]:
    names = [f"lt-{level}-{i}-{secrets.token_hex(2)}" for i in range(runs)]
    assert all(SLUG.match(n) for n in names) and len(set(names)) == runs, names
    return names


def plan_text(
    base: str,
    level: int,
    names: list[str],
    mix: list[dict[str, str]],
    cost: float | None,
    source: str,
) -> str:
    lines = [
        f"target: {base}",
        f"mix source: {source}",
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


def table(
    level: int,
    window: tuple[datetime, datetime],
    runs: int,
    finals: dict[str, str],
    stats: dict[str, Any] | None,
) -> str:
    agg = (stats or {}).get("aggregates", {})
    run_s, wait = agg.get("run_s") or {}, agg.get("queue_wait_s") or {}
    statuses = list(finals.values())
    row = [
        level, window[0].isoformat(), window[1].isoformat(), runs, statuses.count("scored"), statuses.count("failed"), agg.get("runs_per_hour"),
        run_s.get("p50"), run_s.get("p95"), wait.get("p50"), wait.get("p95"),
        agg.get("failures_by_class"), agg.get("sqlite_locked_errors"), None, None, None,
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
    finished = utcnow()  # the last run reached scored or failed, or the timeout hit

    response = client.get(
        "/api/admin/stats",
        params={"since": started.isoformat()},
        headers={"X-Bazaar-Admin-Token": token},
    )
    stats = response.json() if response.status_code == 200 else None
    if stats is None:
        print(f"stats unavailable ({response.status_code})", file=out)

    markdown = table(args.level, (started, finished), len(names), finals, stats)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"{args.level}-{started:%Y%m%dT%H%M%SZ}"
    result = {
        "level": args.level,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
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
    source = p.add_mutually_exclusive_group()
    source.add_argument("--mix", type=Path, help=f"a JSON mix (default {DEFAULT_MIX.name})")
    source.add_argument(
        "--mix-from-house-feeder",
        type=Path,
        metavar="PATH",
        help="a house_feeder.py; only its strategies() is used",
    )
    p.add_argument(
        "--admin-token-file",
        type=Path,
        metavar="PATH",
        help="a file only its owner can read; wins over BAZAAR_ADMIN_TOKEN",
    )
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
    out: TextIO | None = None,
    **clocks: Any,
) -> int:
    args = parser().parse_args(argv)
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    try:
        base = staging_base(args.url)
        if not 1 <= args.level <= 32:
            raise Refused("--level must be 1 to 32 (BAZAAR_MAX_CONCURRENT's range)")
        runs = 2 * args.level if args.runs is None else args.runs
        if not 1 <= runs <= args.max_runs:
            raise Refused(f"refusing {runs} runs: must be 1 to --max-runs ({args.max_runs})")
        if args.mix_from_house_feeder:
            feeder = args.mix_from_house_feeder
            mix = load_feeder_mix(feeder, runs)
            source = f"house feeder {feeder} (strategies(), instructions only)"
        else:
            mix_path = args.mix or DEFAULT_MIX
            mix = load_mix(mix_path)
            source = f"JSON {mix_path}"
        names = run_names(args.level, runs)
        print(plan_text(base, args.level, names, mix, args.cost_per_run, source), file=out)
        if not args.go:
            print("dry run: nothing posted. Add --go to run it.", file=out)
            return 0
        if any(m["instructions"].startswith(PLACEHOLDER) for m in mix):
            raise Refused("the mix still has placeholder strategies; fill them in first")
        if args.admin_token_file:
            token = read_token_file(args.admin_token_file)
        else:
            token = env.get("BAZAAR_ADMIN_TOKEN", "").strip()
        if not token:
            raise Refused(
                "no admin token: pass --admin-token-file or set BAZAAR_ADMIN_TOKEN "
                "(needed for /api/admin/stats)"
            )
    except Refused as exc:
        print(f"REFUSED: {exc}", file=out)
        return 2

    with client or staging_client(base) as http:
        return run(args, client=http, token=token, out=out, names=names, mix=mix, **clocks)


if __name__ == "__main__":
    sys.exit(main())

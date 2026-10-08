"""Live smoke test for the conference deploy.

    python scripts/live_smoke.py BASE_URL [--submit] [--timeout 300]

By default it is read-only: the form page, the heading font and the board's shape. With --submit
it also enters one small strategy, waits for it to be scored and checks its row on the board.
BASE_URL is required (argument or BASE_URL environment variable). Prints one PASS or FAIL line
and exits 0 on PASS, 1 on FAIL, 2 on bad usage. Needs only httpx; it uses no token or secret.
"""

import argparse
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

SLUG = re.compile(r"^[a-z0-9-]{3,40}$")
INSTRUCTIONS = "Buy 10 shares of KO on the first trading day and hold to the end."
FONT = "/fonts/DwightMedium.woff2"


class SmokeFailure(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason


def smoke_name() -> str:
    name = f"smoke-{datetime.now(UTC):%Y%m%dt%H%M}-{secrets.token_hex(2)}"
    assert SLUG.match(name), name
    return name


def _detail(response: httpx.Response) -> str:
    """A short, plain reason: the JSON detail if there is one, else the start of the body."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:200]
    if isinstance(detail, list):
        messages = [str(item.get("msg")) for item in detail if isinstance(item, dict)]
        if messages:
            return "; ".join(messages)[:200]
    text = " ".join(response.text.split())
    return text[:200] or "(empty body)"


def _one_line(text: str) -> str:
    """Server text can hold newlines (tracebacks); the report must stay a single line."""
    return " ".join(text.split())[:300]


def _request(client: httpx.Client, step: str, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        path = httpx.URL(url).path
        raise SmokeFailure(step, f"{method} {path}: {type(error).__name__}: {error}") from error


def _get_ok(client: httpx.Client, step: str, base_url: str, path: str) -> httpx.Response:
    response = _request(client, step, "GET", base_url + path)
    if response.status_code != 200:
        raise SmokeFailure(step, f"GET {path} returned HTTP {response.status_code}")
    return response


def _board_rows(client: httpx.Client, base_url: str) -> list[dict]:
    """The board's rows, checked against the C3 shape: return_pct is a JSON number or null."""
    board = _get_ok(client, "board", base_url, "/api/board")
    try:
        rows = board.json().get("rows")
    except (ValueError, AttributeError):
        rows = None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SmokeFailure("board", "response has no rows list of objects")
    for row in rows:
        value = row.get("return_pct")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise SmokeFailure(
                "board", f"row {row.get('id')} return_pct is not a JSON number: {value!r}"
            )
    return rows


def run(
    client: httpx.Client,
    base_url: str,
    *,
    submit: bool = False,
    timeout: float = 300.0,
    poll: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> tuple[bool, str]:
    base_url = base_url.rstrip("/")
    started = now()
    try:
        _get_ok(client, "page", base_url, "/submit")
        _get_ok(client, "font", base_url, FONT)
        if not submit:
            rows = _board_rows(client, base_url)
            return True, f"PASS read-only {base_url} rows={len(rows)} in {now() - started:.0f}s"

        name = smoke_name()
        response = _request(
            client,
            "submit",
            "POST",
            base_url + "/api/submissions",
            json={"name": name, "handle": None, "instructions": INSTRUCTIONS},
        )
        if response.status_code != 201:
            raise SmokeFailure("submit", f"HTTP {response.status_code}: {_detail(response)}")
        try:
            submission_id = response.json()["id"]
        except (ValueError, KeyError, TypeError):
            submission_id = None
        if not isinstance(submission_id, str) or not submission_id:
            raise SmokeFailure("submit", "201 response has no string id")

        deadline = now() + timeout
        status, day, rank = "unknown", None, None
        problem = None  # why the last poll told us nothing, for the timeout reason
        while True:
            try:
                response = client.get(f"{base_url}/api/submissions/{submission_id}")
            except httpx.HTTPError as error:
                response = None  # transient: keep polling until the deadline
                problem = type(error).__name__
            if response is not None and response.status_code >= 500:
                problem = f"HTTP {response.status_code}"
            elif response is not None:
                if response.status_code == 404:
                    raise SmokeFailure("poll", f"submission {submission_id} not found (HTTP 404)")
                if 400 <= response.status_code < 500:
                    raise SmokeFailure("poll", f"HTTP {response.status_code}: {_detail(response)}")
                if response.status_code < 300:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        problem = "response was not a JSON object"
                        body = {}
                    else:
                        problem = None
                    status, day, rank = (
                        body.get("status", status),
                        body.get("day"),
                        body.get("rank"),
                    )
                    if status == "scored":
                        break
                    if status == "failed":
                        raise SmokeFailure(
                            "run", f"run failed: {body.get('error') or 'no error given'}"
                        )
            if now() >= deadline:
                last = f"; last poll: {problem}" if problem else ""
                raise SmokeFailure(
                    "poll", f"timed out after {timeout:g}s in status {status} (day {day}{last})"
                )
            sleep(poll)

        rows = _board_rows(client, base_url)
        row = next((r for r in rows if isinstance(r, dict) and r.get("id") == submission_id), None)
        if row is None:
            raise SmokeFailure("board", f"row {submission_id} is not on the board")
        if row.get("status") != "scored":
            raise SmokeFailure(
                "board", f"row {submission_id} has status {row.get('status')}, expected scored"
            )
        return_pct = row.get("return_pct")
        if isinstance(return_pct, bool) or not isinstance(return_pct, (int, float)):
            raise SmokeFailure("board", f"return_pct is not a JSON number: {return_pct!r}")
    except SmokeFailure as failure:
        return False, f"FAIL {failure.step}: {_one_line(failure.reason)}"

    elapsed = now() - started
    return True, (
        f"PASS submit {name} id={submission_id} return_pct={return_pct} rank={rank} "
        f"in {elapsed:.0f}s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", nargs="?", default=os.environ.get("BASE_URL"))
    parser.add_argument(
        "--submit", action="store_true", help="also enter a strategy and wait for its score"
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for scoring")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("BASE_URL is required (argument or environment variable)")

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            ok, line = run(client, args.base_url, submit=args.submit, timeout=args.timeout)
    except Exception as error:  # noqa: BLE001 - last resort: one FAIL line, never a traceback
        ok, line = False, f"FAIL internal: {type(error).__name__}: {_one_line(str(error))}"
    print(line)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).parents[1] / "live_smoke.py"
spec = importlib.util.spec_from_file_location("live_smoke", SCRIPT)
live_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_smoke)

BASE = "https://bazaar.example"


class Clock:
    """Fake monotonic clock; sleeping advances it instantly."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def server(
    statuses=("queued", "running", "scored"),
    *,
    fonts=200,
    post=None,
    error=None,
    return_pct=0.6011,
    board_status="scored",
    forbid_post=False,
):
    """A fake C3 service. `statuses` is what successive polls return; the last one repeats."""
    seen = {"polls": 0, "posted": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert not (forbid_post and request.method == "POST"), "read-only mode must not POST"
        path = request.url.path
        if path == "/submit":
            return httpx.Response(200, text="<html>form</html>")
        if path == "/fonts/DwightMedium.woff2":
            return httpx.Response(fonts, content=b"wOF2")
        if path == "/api/submissions" and request.method == "POST":
            seen["posted"] = json.loads(request.content)
            if post is not None:
                return post
            return httpx.Response(201, json={"id": "sub-1", "status": "queued", "position": 2})
        if path == "/api/submissions/sub-1":
            status = statuses[min(seen["polls"], len(statuses) - 1)]
            seen["polls"] += 1
            if isinstance(status, httpx.Response):
                return status
            return httpx.Response(
                200,
                json={
                    "id": "sub-1",
                    "name": seen["posted"]["name"],
                    "status": status,
                    "day": {"queued": 0, "running": 4}.get(status, 10),
                    "position": 1 if status == "queued" else None,
                    "return_pct": return_pct if status == "scored" else None,
                    "rank": 2 if status == "scored" else None,
                    "error": error,
                },
            )
        if path == "/api/board":
            rows = [
                {"id": "base-cash", "status": "scored", "return_pct": 0.0},
                {"id": "sub-1", "status": board_status, "return_pct": return_pct},
            ]
            return httpx.Response(200, json={"window": {}, "rows": rows, "events": []})
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def run(client, **kwargs):
    """Submit mode, the full POST -> poll -> board flow."""
    clock = Clock()
    options = {"timeout": 300.0, "poll": 3.0, "sleep": clock.sleep, "now": clock.now} | kwargs
    return live_smoke.run(client, BASE, submit=True, **options)


def read_only(client):
    clock = Clock()
    return live_smoke.run(client, BASE + "/", sleep=clock.sleep, now=clock.now)


def test_happy_path_passes():
    client, seen = server()
    ok, line = run(client)

    assert ok, line
    assert re.fullmatch(
        r"PASS submit smoke-\d{8}t\d{4}-[0-9a-f]{4} id=sub-1 return_pct=0\.6011 rank=2 in \d+s",
        line,
    )
    assert seen["posted"]["handle"] is None
    assert len(seen["posted"]["instructions"]) >= 20


def test_failed_run_fails_with_its_error():
    client, _ = server(("queued", "failed"), error="market data unavailable")
    ok, line = run(client)

    assert not ok
    assert line == "FAIL run: run failed: market data unavailable"


def test_timeout_fails_with_last_status_and_day():
    client, _ = server(("queued", "running"))
    ok, line = run(client, timeout=30.0)

    assert not ok
    assert line == "FAIL poll: timed out after 30s in status running (day 4)"


def test_transient_errors_while_polling_are_tolerated():
    flaky = httpx.Response(503, text="<html>upstream busy</html>")
    client, _ = server(("queued", flaky, flaky, "scored"))

    assert run(client)[0]


def test_unknown_submission_while_polling_fails():
    client, _ = server((httpx.Response(404, json={"detail": "not found"}),))
    ok, line = run(client)

    assert (ok, line) == (False, "FAIL poll: submission sub-1 not found (HTTP 404)")


def test_string_return_pct_on_the_board_fails():
    client, _ = server(return_pct="0.6011")
    ok, line = run(client)

    assert not ok
    assert line == "FAIL board: row sub-1 return_pct is not a JSON number: '0.6011'"


def test_boolean_return_pct_fails_and_unscored_board_row_fails():
    client, _ = server(return_pct=True)
    assert run(client)[1] == "FAIL board: row sub-1 return_pct is not a JSON number: True"

    client, _ = server(board_status="running")
    assert run(client)[1] == "FAIL board: row sub-1 has status running, expected scored"


def test_missing_font_fails_at_the_font_step():
    client, _ = server(fonts=404)

    assert run(client) == (False, "FAIL font: GET /fonts/DwightMedium.woff2 returned HTTP 404")


def test_post_429_fails_with_the_detail():
    limited = httpx.Response(429, json={"detail": "Too many submissions. Try again later."})
    client, _ = server(post=limited)

    assert run(client) == (
        False,
        "FAIL submit: HTTP 429: Too many submissions. Try again later.",
    )


def test_post_422_list_and_html_errors_are_summarised():
    invalid = httpx.Response(
        422, json={"detail": [{"loc": ["body", "name"], "msg": "name taken"}, {"msg": "bad"}]}
    )
    client, _ = server(post=invalid)
    assert run(client)[1] == "FAIL submit: HTTP 422: name taken; bad"

    page = httpx.Response(502, text="<html>" + "x" * 1000 + "</html>")
    client, _ = server(post=page)
    line = run(client)[1]
    assert line.startswith("FAIL submit: HTTP 502: <html>xxx")
    assert len(line) < 260


def test_network_error_fails_at_the_first_step():
    def broken(request):
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(broken))

    assert run(client) == (False, "FAIL page: GET /submit: ConnectError: connection refused")


def test_generated_names_are_valid_slugs_and_unique():
    first, second = live_smoke.smoke_name(), live_smoke.smoke_name()

    assert re.fullmatch(r"^[a-z0-9-]{3,40}$", first)
    assert re.fullmatch(r"smoke-\d{8}t\d{4}-[0-9a-f]{4}", first)
    assert first != second


def test_read_only_passes_without_posting():
    client, seen = server()
    ok, line = read_only(client)

    assert (ok, line) == (True, f"PASS read-only {BASE} rows=2 in 0s")
    assert seen["posted"] is None


def test_read_only_never_posts():
    client, _ = server(forbid_post=True)

    assert read_only(client)[0]


def test_read_only_fails_on_a_string_return_pct_in_any_row():
    client, _ = server(return_pct="0.6011")

    assert read_only(client) == (
        False,
        "FAIL board: row sub-1 return_pct is not a JSON number: '0.6011'",
    )


def test_read_only_accepts_null_return_pct_for_unscored_rows():
    client, _ = server(return_pct=None, board_status="running")

    assert read_only(client)[0]


def test_missing_base_url_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["live_smoke.py"])
    with pytest.raises(SystemExit) as exit_info:
        live_smoke.main()

    assert exit_info.value.code == 2
    assert "BASE_URL is required" in capsys.readouterr().err


def test_multi_line_server_text_gives_exactly_one_line():
    error = "ModelHTTPError: status_code: 502\nTraceback (most recent call last):\n  File x"
    client, _ = server(("failed",), error=error)
    ok, line = run(client)

    assert not ok and "\n" not in line
    assert (
        line
        == "FAIL run: run failed: ModelHTTPError: status_code: 502 Traceback (most recent call last): File x"
    )

    taken = httpx.Response(409, json={"detail": "taken\nby another"})
    client, _ = server(post=taken)
    assert run(client)[1] == "FAIL submit: HTTP 409: taken by another"


def test_non_object_poll_body_fails_in_one_line_without_raising():
    client, _ = server((httpx.Response(200, json=[]),))
    ok, line = run(client, timeout=9.0)

    assert (ok, line) == (
        False,
        (
            "FAIL poll: timed out after 9s in status unknown "
            "(day None; last poll: response was not a JSON object)"
        ),
    )


def test_timeout_under_persistent_5xx_names_the_last_poll_problem():
    client, _ = server((httpx.Response(503, text="busy"),))
    assert run(client, timeout=9.0) == (
        False,
        "FAIL poll: timed out after 9s in status unknown (day None; last poll: HTTP 503)",
    )


def test_unexpected_errors_become_one_fail_line(monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise RuntimeError("boom\nsecond line")

    monkeypatch.setattr(live_smoke, "run", explode)
    monkeypatch.setattr(sys, "argv", ["live_smoke.py", BASE])
    with pytest.raises(SystemExit) as exit_info:
        live_smoke.main()

    assert exit_info.value.code == 1
    assert capsys.readouterr().out == "FAIL internal: RuntimeError: boom second line\n"

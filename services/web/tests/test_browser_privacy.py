"""Browser privacy proof for submit.html's telemetry (B1).

Serves the real web app, drives headless Chromium through a submission with a sentinel in every
field, captures what the page exports to Logfire (the export is intercepted, never sent) and
checks that no sentinel leaves the browser. Needs Playwright and its Chromium:

    uv run --with playwright==1.63.0 python -m pytest services/web/tests/test_browser_privacy.py

(`python -m pytest`: a venv whose pytest entry point is a shell shim would otherwise run pytest
outside the --with overlay, and the proof would skip.)

Without Playwright or a launchable Chromium (plain `uv run pytest`, the CI test job) every test
here SKIPS with a named reason; it never passes silently. With BAZAAR_REQUIRE_BROWSER_PROOF=1
(the CI browser-privacy job) a missing browser FAILS instead.

Email, company, role and instructions are private: any trace of them in an export fails. Name
and handle are public (the board shows them): a trace of them is reported as a NOTE warning.
"""

import gzip
import json
import os
import re
import socket
import threading
import time
import warnings
from urllib.parse import quote, quote_plus, urlparse

import pytest
import uvicorn
from bazaar_web.app import create_app
from bazaar_web.settings import Settings

try:
    from playwright import sync_api
except ImportError:
    sync_api = None

RUN_LOCALLY = (
    "browser privacy proof: run locally with "
    "uv run --with playwright==1.63.0 python -m pytest services/web/tests/test_browser_privacy.py"
)


def browser_unavailable() -> str | None:
    """Why the proof cannot run here, or None when Playwright and Chromium both work."""
    if sync_api is None:
        return "Playwright is not installed"
    try:
        with sync_api.sync_playwright() as playwright:
            playwright.chromium.launch().close()
    except sync_api.Error as error:
        return f"Chromium cannot launch ({str(error).splitlines()[0]})"
    return None


UNAVAILABLE = browser_unavailable()
REQUIRED = os.environ.get("BAZAAR_REQUIRE_BROWSER_PROOF") == "1"
pytestmark = pytest.mark.skipif(
    UNAVAILABLE is not None and not REQUIRED, reason=f"{RUN_LOCALLY}: {UNAVAILABLE}"
)


@pytest.fixture(autouse=True)
def browser_required():
    """Reached without a browser only when the proof is required: then it fails, never skips."""
    if UNAVAILABLE is not None:
        pytest.fail(
            f"BAZAAR_REQUIRE_BROWSER_PROOF=1 but the browser privacy proof cannot run: "
            f"{UNAVAILABLE}",
            pytrace=False,
        )


class PublicFieldNote(UserWarning):
    """A public field (name or handle) seen in an export: reported, not a failure."""


TOKEN_ENV = "BAZAAR_LOGFIRE_BROWSER_TOKEN"
TOKEN = "test-browser-token-SENTINEL-77"  # fake; never a real token
EXPORT_URL = "https://logfire-us.pydantic.dev/v1/traces"
# As on prod: the page then links each run to its dashboard, with the name in the href's query.
DASHBOARD_URL = "https://logfire-us.pydantic.dev/logfire/bazaar-demo/dashboards/strategy?var-strategy={strategy}"
REPLAY_URL = "https://logfire-us.pydantic.dev/v1/replay"
# Replay uploads start after 10 s of session, then every 5 s while active, plus on pagehide.
SECOND_INTERACTION_AT = 11.0
STAY_ON_PAGE = 16.0
# Text that session replay must mask on /submit (maskAllText is on everywhere but the board).
UNMASKED_TEXT = ("Enter your trading agent", "use your email only to contact you")

PRIVATE = ("email", "company", "role", "instructions")
FIELDS = {
    "name": "name-sentinel-53",
    "handle": "HandleSentinel-29",
    "email": "zz-sentinel-mail@example.org",
    "company": "ZebraSentinel Holdings",
    "role": "Chief Sentinel Officer",
    "instructions": "Buy 10 shares of KO on day one, INSTR-SENTINEL-41, and hold to the end.",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def serve(tmp_path, monkeypatch, helpers):
    """Start the real app on 127.0.0.1 in a thread; yields a function taking the token or None."""
    runners, servers = [], []

    def start(token: str | None) -> str:
        monkeypatch.setenv("BAZAAR_RUNNER_TOKEN", "runner-token")
        monkeypatch.setenv("BAZAAR_RUNS_DIR", str(tmp_path / "runs"))
        monkeypatch.setenv("BAZAAR_WEB_DB", str(tmp_path / "data" / "web.sqlite3"))
        monkeypatch.setenv("BAZAAR_LOGFIRE_DASHBOARD_URL", DASHBOARD_URL)
        if token is None:
            monkeypatch.delenv(TOKEN_ENV, raising=False)
        else:
            monkeypatch.setenv(TOKEN_ENV, token)
        runner = helpers.FakeRunner()  # scores at once; the page still polls its status
        runners.append(runner)
        app = create_app(Settings.from_env(), run_submission=runner)
        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        helpers.wait_for(lambda: server.started, timeout=10)
        servers.append((server, thread))
        return f"http://127.0.0.1:{port}"

    yield start
    for runner in runners:
        runner.gate.set()
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def browser():
    with sync_api.sync_playwright() as playwright:
        chromium = playwright.chromium.launch()  # launchability is checked by the skipif
        yield chromium
        chromium.close()


class Traffic:
    """Every request the page makes: Logfire requests are captured and answered, the app's own
    requests pass through, and anything else external is aborted (offline)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.logfire: list[dict] = []
        self.external: list[str] = []
        self.same_origin: list[dict] = []

    def handle(self, route) -> None:
        request = route.request
        if request.url.startswith("https://logfire-us.pydantic.dev/"):
            body = request.post_data_buffer or b""
            headers = request.all_headers()
            if body[:2] == b"\x1f\x8b":  # gzip (replay chunks are fflate-compressed)
                body = gzip.decompress(body)
            self.logfire.append(
                {"method": request.method, "url": request.url, "headers": headers, "body": body}
            )
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif request.url.startswith(self.base):
            self.same_origin.append(
                {"method": request.method, "url": request.url, "headers": request.all_headers()}
            )
            route.continue_()
        else:
            self.external.append(request.url)
            route.abort()

    @property
    def exports(self) -> list[dict]:
        return [r for r in self.logfire if r["url"].startswith(EXPORT_URL)]

    @property
    def replays(self) -> list[dict]:
        return [r for r in self.logfire if r["url"].startswith(REPLAY_URL)]


def spans(export: dict) -> list[dict]:
    payload = json.loads(export["body"])
    return [
        span
        for resource in payload.get("resourceSpans", [])
        for scope in resource.get("scopeSpans", [])
        for span in scope.get("spans", [])
    ]


def attributes(span: dict) -> dict:
    return {
        a["key"]: next(iter(a.get("value", {}).values()), None) for a in span.get("attributes", [])
    }


def is_submission_post(attrs: dict) -> bool:
    """The fetch span for POST /api/submissions, under the old or the new HTTP conventions."""
    method = attrs.get("http.method") or attrs.get("http.request.method")
    url = str(attrs.get("http.url") or attrs.get("url.full") or "")
    return method == "POST" and url.endswith("/api/submissions")


def forms_of(value: str) -> set[str]:
    """A sentinel as it could appear in a payload: raw, URL-encoded and JSON-escaped, lowercased."""
    return {
        form.lower()
        for form in (value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1])
    }


def replay_chunks(traffic: Traffic) -> list[dict]:
    """Decoded replay uploads: {version, meta: {seq, urls, ...}, events: [rrweb events]}."""
    return [json.loads(replay["body"]) for replay in traffic.replays]


def on_submit_page(chunk: dict) -> bool:
    return any(urlparse(url).path == "/submit" for url in chunk.get("meta", {}).get("urls", []))


def locate(export: dict, needle: str) -> list[str]:
    """Where a sentinel sits in a JSON body: key paths, each prefixed with its span's name."""
    try:
        payload = json.loads(export["body"])
    except ValueError:
        return []
    found = []

    def walk(node, path: str, span: str | None) -> None:
        if isinstance(node, dict):
            span = node.get("name") if "spanId" in node else span
            if "key" in node and "value" in node:  # an OTLP attribute: name it by its key
                path = f"{path}{{{node['key']}}}"
            for key, value in node.items():
                walk(value, f"{path}.{key}", span)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", span)
        elif needle in json.dumps(node).lower():
            found.append(f"span {span!r} at {path}" if span else path)

    walk(payload, "$", None)
    return found


def fill_and_submit(page, base: str) -> tuple[str, float]:
    """Fill every field and submit; returns the submission id and when the page loaded."""
    page.goto(base + "/submit")
    loaded = time.monotonic()
    for field, value in FIELDS.items():
        page.locator(f"#{field}").click()  # a user-interaction span per field, if any are made
        page.keyboard.type(value)
    poll = re.compile(re.escape(base) + r"/api/submissions/[^/?]+$")
    with (
        page.expect_response(lambda r: bool(poll.match(r.url)) and r.request.method == "GET"),
        page.expect_response(
            lambda r: r.url == base + "/api/submissions" and r.request.method == "POST"
        ) as posted,
    ):
        page.locator("#submit").click()  # then the page polls the submission's status once
    assert posted.value.status == 201, posted.value.text()
    return posted.value.json()["id"], loaded


def stay(page, loaded: float) -> None:
    """Keep /submit open long enough for replay uploads, with one more interaction mid-way."""
    page.wait_for_timeout(max(0.0, loaded + SECOND_INTERACTION_AT - time.monotonic()) * 1000)
    page.mouse.click(5, 5)
    page.wait_for_timeout(max(0.0, loaded + STAY_ON_PAGE - time.monotonic()) * 1000)


def response_bodies(responses) -> dict[str, bytes]:
    """Read before leaving the page: Chromium drops bodies once the document is gone."""
    bodies = {}
    for response in responses:
        try:
            bodies[f"{response.request.method} {response.url}"] = response.body()
        except sync_api.Error:
            continue  # redirects and aborted requests have no body
    return bodies


def flush(page, traffic: Traffic, timeout: float = 15.0) -> None:
    """The SDKs upload on visibilitychange/pagehide: leave the page, then wait for both a trace
    export and a replay upload (or the timeout)."""
    page.goto("about:blank")
    deadline = time.monotonic() + timeout
    while not (traffic.exports and traffic.replays) and time.monotonic() < deadline:
        page.wait_for_timeout(100)


def test_no_sentinel_leaves_the_browser_and_the_proof_is_not_vacuous(serve, browser):
    base = serve(TOKEN)
    traffic = Traffic(base)
    context = browser.new_context()
    context.route("**/*", traffic.handle)
    page = context.new_page()
    responses = []
    page.on("response", lambda response: responses.append(response))

    submission_id, loaded = fill_and_submit(page, base)
    # The riskiest attribute is really in the DOM while replay records: the dashboard link,
    # whose href carries the name sentinel in its query.
    link = page.locator("#logfire a.logfire")
    link.wait_for(state="visible", timeout=10_000)
    href = link.get_attribute("href") or ""
    assert f"var-strategy={quote(FIELDS['name'], safe='')}" in href, href
    stay(page, loaded)
    bodies = response_bodies(responses)
    flush(page, traffic)
    context.close()

    unexpected = [r["url"] for r in traffic.logfire if r not in traffic.exports + traffic.replays]
    assert not unexpected, unexpected
    served = bodies.get(f"GET {base}/browser-telemetry.js")
    assert served is not None, "the page loaded no /browser-telemetry.js body"
    module = served.decode()
    assert "import" in module
    # Sampling stays at the SDK default; served code passes no sampling option at all.
    assert "sessionSampleRate" not in module

    # (a) Non-vacuous: real spans, including the submission fetch and a user interaction.
    assert traffic.exports, "no export reached logfire-us"
    exported = [span for export in traffic.exports for span in spans(export)]
    assert exported, "exports carried no spans"
    posts = [span for span in exported if is_submission_post(attributes(span))]
    assert posts, sorted({span["name"] for span in exported})
    assert any("event_type" in attributes(span) for span in exported), sorted(
        {span["name"] for span in exported}
    )

    # Replay is non-vacuous too: decoded /submit chunks with recorded events, including a full
    # DOM snapshot (rrweb type 2), so text missing from them was masked, not just never recorded.
    chunks = replay_chunks(traffic)
    assert chunks, "no replay upload reached logfire-us"
    submit_chunks = [chunk for chunk in chunks if on_submit_page(chunk)]
    assert submit_chunks, [chunk.get("meta", {}).get("urls") for chunk in chunks]
    assert any(chunk.get("events") for chunk in submit_chunks)
    assert any(e.get("type") == 2 for chunk in submit_chunks for e in chunk.get("events", []))
    for text in UNMASKED_TEXT:
        for chunk in submit_chunks:
            assert text.lower() not in json.dumps(chunk).lower(), f"replay recorded {text!r}"

    # (b) No sentinel, in any form, in any export or replay URL, header or (decoded) body.
    for export in traffic.exports:
        assert export["method"] == "POST" and export["url"] == EXPORT_URL
    for export in traffic.logfire:
        haystack = " ".join(
            [
                export["url"],
                json.dumps(export["headers"]),
                export["body"].decode("utf-8", errors="replace"),
            ]
        ).lower()
        kind = "replay chunk" if export in traffic.replays else "trace export"
        for field, value in FIELDS.items():
            for form in forms_of(value):
                if form not in haystack:
                    continue
                where = (
                    f"{kind} {export['url']}: "
                    f"{locate(export, form) or 'URL, headers or non-JSON body'}"
                )
                assert field not in PRIVATE, f"BLOCKER: {field} sentinel {form!r} in {where}"
                warnings.warn(f"NOTE: public {field} {form!r} in {where}", PublicFieldNote)

    # (c) The token authorises exports and is served only by the telemetry module.
    for export in traffic.exports:
        assert export["headers"].get("authorization") == f"Bearer {TOKEN}"
    carriers = [key for key, body in bodies.items() if TOKEN.encode() in body]
    assert carriers == [f"GET {base}/browser-telemetry.js"], carriers

    # (d) The status poll is ignored: no exported span names the submission's own URL.
    for export in traffic.exports:
        assert f"/api/submissions/{submission_id}" not in export["body"].decode(
            "utf-8", errors="replace"
        )

    # (e) The submission POST joins the browser trace.
    post = next(
        r
        for r in traffic.same_origin
        if r["method"] == "POST" and r["url"] == base + "/api/submissions"
    )
    assert re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]", post["headers"].get("traceparent", "")
    )


def test_without_a_token_the_page_sends_no_telemetry_and_still_submits(serve, browser):
    base = serve(None)
    traffic = Traffic(base)
    context = browser.new_context()
    context.route("**/*", traffic.handle)
    page = context.new_page()

    module = context.request.get(base + "/browser-telemetry.js")
    assert module.status == 200
    assert "import" not in module.text()

    _, loaded = fill_and_submit(page, base)  # asserts the 201
    stay(page, loaded)  # long enough that a replay upload would have happened
    flush(page, traffic, timeout=3.0)
    context.close()

    assert traffic.logfire == []  # no /v1/traces, no /v1/replay: nothing reaches logfire-us

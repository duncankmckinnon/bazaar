"""v9 B1: GET /browser-telemetry.js configures the vendored logfire-browser only when the public
frontend token is set, and that token appears in that response and nowhere else."""

import dataclasses
import json
import logging
import re
import shutil
import subprocess

import pytest
from bazaar_web.app import STATIC, create_app
from bazaar_web.browser import IGNORE_URLS, VENDORED, VENDORED_REPLAY
from bazaar_web.settings import Settings
from bazaar_web.telemetry import EXCLUDED_URLS
from fastapi.testclient import TestClient

SENTINEL = "pylf_v1_us_BROWSERSENTINEL0123456789"
VENDOR = STATIC / "vendor" / "logfire-browser-0.25.0.js"
VENDOR_REPLAY = STATIC / "vendor" / "logfire-session-replay-0.5.0.js"
BOARD_HTML = (STATIC / "board.html").read_text()
IGNORES = (
    r"/\/api\/board(\?|$)/",
    r"/\/api\/tickers(\?|$)/",
    r"/\/api\/submissions\/[^/?]+/",
    r"/\/api\/leads(\?|$)/",
    r"/\/api\/admin\//",
)


def module(settings, helpers, token, query=""):
    config = dataclasses.replace(settings, logfire_browser_token=token)
    with TestClient(create_app(config, helpers.FakeRunner())) as client:
        return client.get(f"/browser-telemetry.js{query}")


@pytest.mark.parametrize("token", [None, ""])
@pytest.mark.parametrize("query", ["", "?page=board", "?page=submit"])
def test_unset_serves_only_a_comment(settings, helpers, token, query):
    response = module(settings, helpers, token, query)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-store"
    assert all(line.startswith("//") for line in response.text.strip().splitlines())
    assert "import" not in response.text and "logfire-us" not in response.text
    assert "replay" not in response.text.lower()


def test_set_configures_exactly_the_agreed_options(settings, helpers):
    response = module(settings, helpers, SENTINEL)
    body = response.text

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/javascript")
    assert f'import {{ configure }} from "{VENDORED}";' in body
    assert f"const TOKEN = {json.dumps(SENTINEL)};" in body
    assert 'const TRACE_URL = "https://logfire-us.pydantic.dev/v1/traces";' in body
    assert "traceUrl: TRACE_URL," in body
    assert "traceExporterHeaders: () => ({ Authorization: `Bearer ${TOKEN}` })," in body
    assert '"@opentelemetry/instrumentation-fetch": { ignoreUrls: IGNORE },' in body
    assert '"@opentelemetry/instrumentation-xml-http-request": { ignoreUrls: IGNORE },' in body
    assert "rum: { webVitals: true }," in body
    assert f"const IGNORE = [{', '.join(IGNORES)}];" in body
    for unset in ("serviceName", "serviceNamespace", "environment"):
        assert unset not in body


def test_the_token_is_quoted_as_a_json_string(settings, helpers):
    body = module(settings, helpers, 'a"b\\c</script>').text
    assert 'const TOKEN = "a\\"b\\\\c</script>";' in body


def test_the_token_appears_in_no_other_response_or_log(seeded, helpers, caplog):
    config = dataclasses.replace(seeded, logfire_browser_token=SENTINEL)
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(config, helpers.FakeRunner())) as client,
    ):
        bodies = {
            path: client.get(path)
            for path in ("/", "/submit", "/api/board", "/api/tickers", "/health", "/api/health",
                         "/static/vendor/logfire-browser-0.25.0.js", "/nope")
        }  # fmt: skip
        assert SENTINEL in client.get("/browser-telemetry.js").text

    for path, response in bodies.items():
        assert SENTINEL not in response.text, path
        assert SENTINEL not in json.dumps(dict(response.headers)), path
    assert SENTINEL not in caplog.text
    assert SENTINEL not in repr(config)


def test_the_token_comes_from_the_env(monkeypatch):
    monkeypatch.delenv("BAZAAR_LOGFIRE_BROWSER_TOKEN", raising=False)
    assert Settings.from_env().logfire_browser_token is None
    monkeypatch.setenv("BAZAAR_LOGFIRE_BROWSER_TOKEN", "   ")
    assert Settings.from_env().logfire_browser_token is None
    monkeypatch.setenv("BAZAAR_LOGFIRE_BROWSER_TOKEN", f" {SENTINEL} ")
    assert Settings.from_env().logfire_browser_token == SENTINEL


def test_the_route_is_not_traced_by_the_backend():
    assert any(re.search(p, "http://x/browser-telemetry.js") for p in EXCLUDED_URLS)
    assert any(re.search(p, "http://x/browser-telemetry.js?v=1") for p in EXCLUDED_URLS)


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("/api/board", True), ("/api/board?since=1", True), ("/api/tickers", True),
        ("/api/submissions/abc", True), ("/api/submissions/abc?x=1", True),
        ("/api/submissions", False), ("/api/submissions/", False),
        # The factory control's admin requests carry the admin token header: never traced.
        ("/api/admin/house", True), ("/api/admin/house/pause", True),
        ("/api/admin/submissions/abc/rerun", True), ("/api/administer", False),
        ("/api/boards", False),
        # The form's email step: personal data.
        ("/api/leads", True), ("/api/leads?x=1", True), ("/api/leadsx", False),
    ],
)  # fmt: skip
def test_the_ignore_patterns(path, ignored):
    for url in (path, f"https://bazaar.example{path}"):
        assert any(re.search(p, url) for p in IGNORE_URLS) is ignored, url


def test_the_page_loads_the_module():
    page = (STATIC / "board.html").read_text()
    head = page.split("</head>", 1)[0]
    assert '<script type="module" src="/browser-telemetry.js?page=board"></script>' in head


def test_the_vendored_bundle_is_one_self_contained_module():
    text = VENDOR.read_text(encoding="utf-8")

    assert text.startswith("/*!\n * @pydantic/logfire-browser 0.25.0 (MIT), bundled by esbuild")
    header = text.split("*/", 1)[0]
    assert "Apache License" in header and "MIT License" in header
    assert "@opentelemetry/auto-instrumentations-web@" in header and "web-vitals@" in header
    code = text[len(header) :]
    for bare in ('from"@', 'from "@', 'import("@', "import('@", "import(`@", 'from"web-vitals'):
        assert bare not in code, bare
    assert re.search(r"\bimport\(", code) is None  # no lazy chunk left to fetch
    assert re.search(r"export\{\w+ as configure\};?\s*$", code)


NODE = shutil.which("node")


# ---------- session replay (B2) ----------


def unmask_line(body):
    (line,) = re.findall(r"^const UNMASK_BOARD = .*$", body, re.MULTILINE)
    return line


def test_replay_uses_the_vendored_sdk_and_the_trace_url_origin(settings, helpers):
    body = module(settings, helpers, SENTINEL, "?page=board").text

    assert "sessionReplay: {" in body
    assert f'load: () => import("{VENDORED_REPLAY}"),' in body
    assert 'replayUrl: new URL("/v1/replay", TRACE_URL).href,' in body
    assert "headers: () => ({ Authorization: `Bearer ${TOKEN}` })," in body
    assert 'maskAllText: !(UNMASK_BOARD && location.pathname === "/"),' in body


@pytest.mark.parametrize("query", ["", "?page=board", "?page=submit"])
def test_replay_keeps_the_sdk_defaults(settings, helpers, query):
    body = module(settings, helpers, SENTINEL, query).text
    # Default sampling (every session) is the policy; inputs stay masked; no capture changes.
    for forbidden in ("sessionSampleRate", "onErrorSampleRate", "maskAllInputs", "capture",
                      "serviceName", "serviceNamespace", "environment", "redactUrlPatterns",
                      "blockSelector", "maskTextSelector", "rum: { webVitals: true, session"):  # fmt: skip
        assert forbidden not in body, forbidden


def test_only_page_board_exactly_unmasks_board_text(settings, helpers):
    assert unmask_line(module(settings, helpers, SENTINEL, "?page=board").text) == (
        "const UNMASK_BOARD = true;"
    )
    for query in ("", "?page=", "?page=submit", "?page=BOARD", "?page=board&page=x",
                  "?page=x&page=board", "?page=xyz", "?page=board%20", "?pages=board"):  # fmt: skip
        body = module(settings, helpers, SENTINEL, query).text
        assert unmask_line(body) == "const UNMASK_BOARD = false;", query


def test_the_submit_page_gets_a_masked_module(settings, helpers):
    config = dataclasses.replace(settings, logfire_browser_token=SENTINEL)
    with TestClient(create_app(config, helpers.FakeRunner())) as client:
        page = client.get("/submit").text
        sources = re.findall(r'<script[^>]*\bsrc="(/browser-telemetry\.js[^"]*)"', page)
        # evals adds the submit page's tag; until it is on this branch, the plain path.
        src = sources[0] if sources else "/browser-telemetry.js"
        body = client.get(src).text

    assert unmask_line(body) == "const UNMASK_BOARD = false;"


def test_the_board_asks_for_its_page_exactly():
    sources = re.findall(r'<script[^>]*\bsrc="([^"]*browser-telemetry[^"]*)"', BOARD_HTML)
    assert sources == ["/browser-telemetry.js?page=board"]


def test_the_vendored_replay_bundle_is_one_self_contained_module():
    text = VENDOR_REPLAY.read_text(encoding="utf-8")

    assert text.startswith(
        "/*!\n * @pydantic/logfire-session-replay 0.5.0 (MIT), bundled by esbuild"
    )
    header = text.split("*/", 1)[0]
    for package in ("@rrweb/record@2.1.1", "fflate@0.8.3"):
        assert package in header
    assert "rrweb-io/rrweb/graphs/contributors" in header  # rrweb's own MIT notice
    assert "no license file" not in header
    code = text[len(header) :]
    for bare in ('from"@', 'from "@', 'import("@', "import('@", "import(`@", 'from"fflate'):
        assert bare not in code, bare
    assert re.search(r"\bimport\(", code) is None
    assert re.search(r"export\{\w+ as startSessionReplay\};?\s*$", code)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_module_runs_in_node_against_a_stub(settings, helpers, tmp_path):
    """The served module, with the vendored import pointed at a stub that records the options."""
    board = module(settings, helpers, SENTINEL, "?page=board").text
    plain = module(settings, helpers, SENTINEL).text
    (tmp_path / "stub.mjs").write_text(
        "export function configure(o) { (globalThis.options ??= []).push(o); }"
    )
    (tmp_path / "board.mjs").write_text(board.replace(VENDORED, "./stub.mjs"))
    (tmp_path / "plain.mjs").write_text(plain.replace(VENDORED, "./stub.mjs"))
    (tmp_path / "run.mjs").write_text(
        """
        // maskAllText is computed when the module runs, from the page's own path.
        globalThis.location = { pathname: "/" };
        await import("./board.mjs?a");
        globalThis.location = { pathname: "/submit" };
        await import("./board.mjs?b");
        globalThis.location = { pathname: "/" };
        await import("./plain.mjs");
        const [o, onSubmit, plain] = globalThis.options;
        const r = o.sessionReplay;
        const fetchIgnore = o.autoInstrumentations["@opentelemetry/instrumentation-fetch"].ignoreUrls;
        const paths = ["/api/board", "/api/submissions/abc", "/api/submissions",
                       "/api/admin/house", "/api/leaderboard"];
        console.log(JSON.stringify({
          keys: Object.keys(o).sort(), traceUrl: o.traceUrl, headers: o.traceExporterHeaders(),
          rum: o.rum, replayKeys: Object.keys(r).sort(), replayUrl: r.replayUrl,
          replayHeaders: r.headers(), loads: String(r.load),
          masked: [o, onSubmit, plain].map((x) => x.sessionReplay.maskAllText),
          sameXhr: o.autoInstrumentations["@opentelemetry/instrumentation-xml-http-request"].ignoreUrls === fetchIgnore,
          ignored: paths.map((p) => fetchIgnore.some((r) => r.test("https://x" + p))),
        }));
        """
    )
    out = subprocess.run(
        [NODE, "run.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True
    )
    result = json.loads(out.stdout)

    assert result == {
        "keys": [
            "autoInstrumentations",
            "rum",
            "sessionReplay",
            "traceExporterHeaders",
            "traceUrl",
        ],
        "traceUrl": "https://logfire-us.pydantic.dev/v1/traces",
        "headers": {"Authorization": f"Bearer {SENTINEL}"},
        "rum": {"webVitals": True},
        "replayKeys": ["headers", "load", "maskAllText", "replayUrl"],
        "replayUrl": "https://logfire-us.pydantic.dev/v1/replay",
        "replayHeaders": {"Authorization": f"Bearer {SENTINEL}"},
        "loads": f'() => import("{VENDORED_REPLAY}")',
        # board page on "/": text unmasked; the board module on /submit, or the plain module on
        # "/": masked.
        "masked": [False, True, True],
        "sameXhr": True,
        "ignored": [True, True, False, True, False],
    }

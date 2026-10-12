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
from bazaar_web.browser import IGNORE_URLS, VENDORED
from bazaar_web.settings import Settings
from bazaar_web.telemetry import EXCLUDED_URLS
from fastapi.testclient import TestClient

SENTINEL = "pylf_v1_us_BROWSERSENTINEL0123456789"
VENDOR = STATIC / "vendor" / "logfire-browser-0.25.0.js"
IGNORES = (
    r"/\/api\/board(\?|$)/",
    r"/\/api\/tickers(\?|$)/",
    r"/\/api\/submissions\/[^/?]+/",
)


def module(settings, helpers, token):
    config = dataclasses.replace(settings, logfire_browser_token=token)
    with TestClient(create_app(config, helpers.FakeRunner())) as client:
        return client.get("/browser-telemetry.js")


@pytest.mark.parametrize("token", [None, ""])
def test_unset_serves_only_a_comment(settings, helpers, token):
    response = module(settings, helpers, token)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-store"
    assert all(line.startswith("//") for line in response.text.strip().splitlines())
    assert "import" not in response.text and "logfire-us" not in response.text


def test_set_configures_exactly_the_agreed_options(settings, helpers):
    response = module(settings, helpers, SENTINEL)
    body = response.text

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/javascript")
    assert f'import {{ configure }} from "{VENDORED}";' in body
    assert f"const TOKEN = {json.dumps(SENTINEL)};" in body
    assert 'traceUrl: "https://logfire-us.pydantic.dev/v1/traces",' in body
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
        ("/api/admin/submissions/abc/rerun", False), ("/api/admin/stats", False),
        ("/api/boards", False),
    ],
)  # fmt: skip
def test_the_ignore_patterns(path, ignored):
    for url in (path, f"https://bazaar.example{path}"):
        assert any(re.search(p, url) for p in IGNORE_URLS) is ignored, url


def test_the_page_loads_the_module():
    page = (STATIC / "board.html").read_text()
    head = page.split("</head>", 1)[0]
    assert '<script type="module" src="/browser-telemetry.js"></script>' in head


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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_module_runs_in_node_against_a_stub(settings, helpers, tmp_path):
    """The served module, with the vendored import pointed at a stub that records the options."""
    body = module(settings, helpers, SENTINEL).text
    (tmp_path / "stub.mjs").write_text("export function configure(o) { globalThis.options = o; }")
    (tmp_path / "page.mjs").write_text(body.replace(VENDORED, "./stub.mjs"))
    (tmp_path / "run.mjs").write_text(
        """
        await import("./page.mjs");
        const o = globalThis.options;
        const fetchIgnore = o.autoInstrumentations["@opentelemetry/instrumentation-fetch"].ignoreUrls;
        const paths = ["/api/board", "/api/submissions/abc", "/api/submissions", "/api/admin/x"];
        console.log(JSON.stringify({
          keys: Object.keys(o).sort(), traceUrl: o.traceUrl, headers: o.traceExporterHeaders(),
          rum: o.rum, sameXhr: o.autoInstrumentations["@opentelemetry/instrumentation-xml-http-request"].ignoreUrls === fetchIgnore,
          ignored: paths.map((p) => fetchIgnore.some((r) => r.test("https://x" + p))),
        }));
        """
    )
    out = subprocess.run(
        [NODE, "run.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True
    )
    result = json.loads(out.stdout)

    assert result == {
        "keys": ["autoInstrumentations", "rum", "traceExporterHeaders", "traceUrl"],
        "traceUrl": "https://logfire-us.pydantic.dev/v1/traces",
        "headers": {"Authorization": f"Bearer {SENTINEL}"},
        "rum": {"webVitals": True},
        "sameXhr": True,
        "ignored": [True, True, False, False],
    }

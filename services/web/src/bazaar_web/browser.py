"""Browser telemetry (v9 B1): the module GET /browser-telemetry.js serves to the board page.

With BAZAAR_LOGFIRE_BROWSER_TOKEN set, the module configures the vendored @pydantic/logfire-browser
to send the page's traces (page load, interactions, same-origin fetches and Web Vitals) to
Logfire's frontend app. The token is the restricted public frontend token: it is meant to be seen
by browsers, but only in this response. Unset, the module is a comment and the page is unchanged.
"""

import json

VENDORED = "/static/vendor/logfire-browser-0.25.0.js"
TRACE_URL = "https://logfire-us.pydantic.dev/v1/traces"
# Like the backend's EXCLUDED_URLS: the board's polls and the form's status poll are not traced.
# Searched in the full URL; POST /api/submissions (no id segment) stays traced and carries
# traceparent, so the backend's span joins the browser trace.
IGNORE_URLS = (
    r"/api/board(\?|$)",
    r"/api/tickers(\?|$)",
    r"/api/submissions/[^/?]+",
)

OFF = "// Browser telemetry is off (BAZAAR_LOGFIRE_BROWSER_TOKEN is not set).\n"

MODULE = """\
// Browser telemetry for Logfire (v9 B1), served by bazaar-web.
import {{ configure }} from "{vendored}";

const TOKEN = {token};
const IGNORE = [{ignore}];

configure({{
  traceUrl: "{trace_url}",
  traceExporterHeaders: () => ({{ Authorization: `Bearer ${{TOKEN}}` }}),
  autoInstrumentations: {{
    "@opentelemetry/instrumentation-fetch": {{ ignoreUrls: IGNORE }},
    "@opentelemetry/instrumentation-xml-http-request": {{ ignoreUrls: IGNORE }},
  }},
  rum: {{ webVitals: true }},
}});
"""


def js_regex(pattern: str) -> str:
    """A JavaScript regex literal for a pattern written without slashes escaped: a slash is
    escaped outside character classes (inside one, as in [^/?], it is already literal)."""
    out, in_class = [], False
    for char in pattern:
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        out.append("\\/" if char == "/" and not in_class else char)
    return "/" + "".join(out) + "/"


def telemetry_module(token: str | None) -> str:
    if not token:
        return OFF
    return MODULE.format(
        vendored=VENDORED,
        token=json.dumps(token),
        ignore=", ".join(js_regex(p) for p in IGNORE_URLS),
        trace_url=TRACE_URL,
    )

"""F3: the board's admin-only factory pause/resume control (source checks; the browser proof is
in test_browser_privacy.py)."""

import re

from bazaar_web.app import STATIC
from bazaar_web.browser import IGNORE_URLS

BOARD = (STATIC / "board.html").read_text()
F3 = next(s for s in BOARD.split("<script>")[1:] if "bazaar.admin" in s).split("</script>")[0]


def test_the_token_is_sent_only_in_the_admin_header():
    # Every request goes through one helper, with the token in the header and nowhere else.
    assert re.findall(r"\bfetch\(", F3) == ["fetch("]
    assert 'return fetch(path, { method, headers: { [HEADER]: token }, cache: "no-store" });' in F3
    assert 'const HEADER = "X-Bazaar-Admin-Token";' in F3
    calls = re.findall(r"admin\(token, \"(GET|POST)\", ([^)]*)\)", F3)
    assert {path for _, path in calls} == {
        "HOUSE",
        'paused ? HOUSE + "/resume" : HOUSE + "/pause"',
    }
    assert 'const HOUSE = "/api/admin/house";' in F3
    for leak in ("token=", "?token", "+ token", "token +", "${token}", "console."):
        assert leak not in F3, leak


def test_the_token_lives_in_session_storage_only():
    assert "sessionStorage" in F3 and "localStorage" not in BOARD
    assert 'const KEY = "bazaar.admin";' in F3
    assert "document.cookie" not in BOARD


def test_the_form_is_a_password_field_cleared_on_submit_and_no_prompt():
    assert 'input.type = "password";' in F3 and 'input.autocomplete = "off";' in F3
    assert F3.index("const token = input.value;") < F3.index('input.value = "";')
    assert "prompt(" not in BOARD
    assert "innerHTML" not in F3
    assert "history.replaceState" in F3 and 'url.searchParams.delete("admin")' in F3


def test_the_public_view_builds_nothing_and_calls_nothing():
    # start(): a saved token is checked first; otherwise only ?admin shows the form. With
    # neither, start() returns without a request or an element.
    start = F3.split("async function start() {", 1)[1].split("\nstart();", 1)[0]
    assert start.strip().startswith("const token = saved.get();\n  if (token) {")
    assert "if (askedFor()) unlockForm();" in start
    assert F3.count("await check(token)") == 3  # start, the form submit and the refresh only
    assert 'new URLSearchParams(location.search).has("admin")' in F3


def test_the_control_is_blocked_from_session_replay_and_admin_fetches_untraced():
    assert F3.count('"adm rr-block"') == 2  # the form and the pill
    assert r"/api/admin/" in IGNORE_URLS

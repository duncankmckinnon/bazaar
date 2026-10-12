import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).parents[1] / "src" / "bazaar_web" / "static" / "submit.html"


def page():
    return PAGE.read_text(encoding="utf-8")


def test_page_exists_with_a_viewport_meta():
    assert PAGE.is_file()
    assert re.search(r'<meta name="viewport" content="width=device-width', page())


def test_the_only_script_src_is_the_same_origin_telemetry_module_in_the_head():
    html = page()
    tags = re.findall(r"<script\b[^>]*\bsrc\s*=[^>]*>", html, re.IGNORECASE)

    assert tags == ['<script type="module" src="/browser-telemetry.js">']
    # A query string (?page=board) unmasks session replay text; the form must never carry one.
    assert "browser-telemetry.js?" not in html
    head = html[: html.index("</head>")]
    assert '<script type="module" src="/browser-telemetry.js"></script>' in head


def test_no_external_scripts_and_only_google_font_urls():
    html = page()

    hosts = {
        re.match(r"https?://([^/\"'\s)]+)", url).group(1)
        for url in re.findall(r"https?://[^\s\"')]+", html)
    }
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}


def test_uses_the_submission_and_leads_apis_and_never_reads_the_board():
    html = page()

    assert "/api/submissions" in html
    assert '"/api/leads"' in html
    assert "/api/board" not in html  # the v11 lede has no dates, so no window fetch
    assert "setInterval" not in html


def test_validates_the_name_slug_and_strategy_length():
    html = page()

    assert "^[a-z0-9-]{3,40}$" in html
    assert "Use 3 to 40 letters, digits or hyphens" in html  # the server's 422 msg, verbatim
    assert re.search(r"MIN_CHARS = 20\b", html)
    assert re.search(r"MAX_CHARS = 4000\b", html)


def test_never_writes_html_from_data():
    html = page()

    assert not re.search(r"innerHTML\s*=", html)
    assert not re.search(r"outerHTML\s*=|insertAdjacentHTML|document\.write", html)
    assert not re.search(r"\beval\s*\(|new Function\s*\(", html)


def test_headings_use_silkscreen_from_google_fonts():
    html = page()

    assert "family=Silkscreen" in html
    assert re.search(r"--display:\s*\"Silkscreen\"", html)


MARKED = ("safeLogfireUrl", "names", "emailOk", "builder")


def run_page(expression):
    """Evaluate an expression in node with the page's pure helpers loaded; return it as JSON."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    sources = re.findall(
        r"^  function (?:toNumber|percent|rankLabel)\(.*?^  }$", page(), re.MULTILINE | re.DOTALL
    )
    for marker in MARKED:
        sources += re.findall(rf"// {marker}:start(.*?)// {marker}:end", page(), re.DOTALL)
    script = "\n".join(sources) + f"\nconsole.log(JSON.stringify({expression}));"
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def run_page_function(name, calls):
    return run_page(f"[{', '.join(calls)}].map(function (x) {{ return {name}(x); }})")


def test_percent_formats_numbers_and_decimal_strings():
    calls = ["0.6011", '"0.6011"', "-1.5", '"-1.5"', "0", "null", '""', '"abc"', "Infinity"]

    assert run_page_function("percent", calls) == [
        "+0.60%",
        "+0.60%",
        "-1.50%",
        "-1.50%",
        "0.00%",
        None,
        None,
        None,
        None,
    ]


def test_rank_label_accepts_positive_whole_numbers_and_digit_strings():
    calls = ["2", '"2"', "0", "1.5", "null", '"x"']

    assert run_page_function("rankLabel", calls) == ["#2", "#2", None, None, None, None]


def test_safe_logfire_url_allows_only_https_on_pydantic_dev():
    good = "https://logfire-us.pydantic.dev/x/y?q=1"
    calls = [
        json.dumps(good),
        "null",
        '""',
        '"javascript:alert(1)"',
        '"http://example.com"',
        '"/relative"',
        '"data:text/html,x"',
        "42",
        '"not a url"',
        '"HTTPS://logfire-us.pydantic.dev/x"',
        '"https://logfire-eu.pydantic.dev/a"',
        '"https://pydantic.dev/a"',
        '"https://evil.example/a"',
        '"https://pydantic.dev.evil.com/a"',
        '"https://logfire-us.pydantic.info/a"',
        '"https://notpydantic.dev/a"',
    ]

    assert run_page_function("safeLogfireUrl", calls) == [
        good,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "HTTPS://logfire-us.pydantic.dev/x",
        "https://logfire-eu.pydantic.dev/a",
        "https://pydantic.dev/a",
        None,
        None,
        None,
        None,
    ]


def test_logfire_link_is_labelled_and_opens_safely():
    html = page()

    assert "See your agent in Logfire" in html
    assert '"noopener noreferrer"' in html
    assert '"_blank"' in html
    assert "// safeLogfireUrl:start" in html and "// safeLogfireUrl:end" in html


PAGE_BACKGROUNDS = ("#5a1d5e", "#2a1230", "#1c1220")  # the gradient's stops, lightest first
FIELD_FILL = "#22121f"
TEXT_TOKENS = ("text", "muted", "dim", "lime", "aqua", "error")
PLACEHOLDER_TOKEN = "dim"


def css_tokens():
    root = re.search(r":root\s*\{(.*?)\}", page(), re.DOTALL).group(1)
    return {
        name: value.lower() for name, value in re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", root)
    }


def contrast(a, b):
    def luminance(color):
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_text_tokens_meet_wcag_aa_on_the_page_and_the_input_fill():
    tokens = css_tokens()

    assert tokens["field"] == FIELD_FILL
    for name in TEXT_TOKENS:
        for background in (*PAGE_BACKGROUNDS, FIELD_FILL):
            assert contrast(tokens[name], background) >= 4.5, (name, background)
    assert contrast(tokens["ink"], tokens["lime"]) >= 4.5  # the button and the pressed chips


def test_placeholder_uses_a_pinned_token_that_meets_wcag_aa_on_the_fill():
    rule = re.search(r"::placeholder\s*\{([^}]*)\}", page()).group(1)

    assert f"color: var(--{PLACEHOLDER_TOKEN})" in rule
    assert "opacity: 1" in rule
    assert contrast(css_tokens()[PLACEHOLDER_TOKEN], FIELD_FILL) >= 4.5


def test_input_and_chip_borders_meet_non_text_contrast_against_page_and_field():
    tokens = css_tokens()

    for background in (*PAGE_BACKGROUNDS, FIELD_FILL):
        assert contrast(tokens["field-line"], background) >= 3, background
    assert re.search(
        r"input\[type=\"text\"\], input\[type=\"email\"\], textarea \{[^}]*border: 1px solid var\(--field-line\)",
        page(),
    )
    assert re.search(r"\.chip \{[^}]*border: 1px solid var\(--field-line\)", page())


def test_page_uses_the_prototype_background_and_no_decorative_text_colours():
    html = page()

    assert (
        "background: radial-gradient(120% 60% at 50% 0%, #5a1d5e 0%, #2a1230 55%, #1c1220 100%)"
    ) in html
    assert not re.search(r"(?<![-\w])color:\s*var\(--(?:card-line|field-line)\)", html)


def text_of(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).replace("&amp;", "&").split())


def test_prototype_copy_is_verbatim():
    text = text_of(page())

    for copy in (
        "Enter your trading agent",
        "Trade any S&P 500 stock for 10 days, starting with $10,000. Two fields, then pick a style.",
        "Strategy name",
        "Lowercase is automatic. Spaces become hyphens.",
        "Work email",
        "We save your email as soon as you leave this field, and use it only to contact winners.",
        "Saved. You're on the list even if you don't submit.",
        "Build your strategy",
        "no typing needed",
        "Aggressiveness",
        "Hold positions for",
        "Stop-loss",
        "Signal",
        "Sectors",
        "Your strategy",
        "Use the example",
        "Written from your choices above. Edit freely; moving a slider rewrites it.",
        (
            "Example of a decent strategy: Each morning, buy about $2,000 of the tech stock with "
            "the strongest 5-day gain, if it is beating SPY. Sell anything that falls 4% below "
            "what you paid. One order a day."
        ),
        "Insert policy",
    ):
        assert copy in text, copy
    assert 'placeholder="e.g. tech-momentum"' in page()
    assert 'placeholder="you@company.com"' in page()


def test_the_example_button_uses_the_example_text_shown_in_the_box():
    box = re.search(r'<div class="example-box">(.*?)</div>', page(), re.DOTALL).group(1)
    shown = text_of(box).removeprefix("Example of a decent strategy: ")

    assert run_page("EXAMPLE") == shown
    assert run_page("next(DEFAULTS, {type: 'example'}).custom") == shown


def test_the_rename_note_is_the_prototypes_and_keyed_on_renamed():
    html = page()

    assert "That name is taken, so you'll play as " in html
    assert ". No need to change it." in html
    assert "body.renamed === true" in html


def input_tag(field_id):
    return re.search(rf'<input id="{field_id}"[^>]*>', page()).group(0)


def test_only_the_name_and_email_fields_remain():
    html = page()

    for gone in ("handle", "company", "role", "contact-purpose", "examples", "window"):
        assert f'id="{gone}"' not in html, gone
    email = input_tag("email")
    for attribute in (
        'type="email"',
        'inputmode="email"',
        'autocomplete="email"',
        'maxlength="254"',
        " required",
    ):
        assert attribute in email, attribute
    assert 'maxlength="40"' in input_tag("name")


def test_email_never_reaches_the_url_and_422_input_is_never_shown():
    html = page()

    assert '<form id="submit-form" method="post"' in html
    assert " name=" not in input_tag("email")
    leads = re.search(r'fetch\("/api/leads", \{(.*?)\n    \}\)', html, re.DOTALL).group(1)
    assert 'method: "POST"' in leads and "body: JSON.stringify({ email: email })" in leads
    assert not re.search(r"/api/leads\?|encodeURIComponent\(email", html)
    show_detail = re.search(r"function showDetail\(.*?\n  \}\n", html, re.DOTALL).group(0)
    assert set(re.findall(r"\bitem\.(\w+)", show_detail)) == {"msg", "loc"}
    assert not re.search(r"\.input\b", html)


def test_server_field_errors_map_to_the_three_fields():
    fields = re.search(r"var fields = \{([^}]*)\}", page()).group(1)

    assert sorted(re.findall(r"\b(\w+): ", fields)) == ["email", "instructions", "name"]


def test_names_normalise_like_the_server():
    cases = {
        " --a  b-- ": ("-a-b-", "a-b"),
        "Tech Momentum": ("tech-momentum", "tech-momentum"),
        "a ": ("a-", "a"),
        "A b!c": ("a-bc", "a-bc"),
        "x\t\ny": ("x-y", "x-y"),
        "--": ("-", ""),
        "dip_buyer 2": ("dipbuyer-2", "dipbuyer-2"),
    }
    calls = [json.dumps(raw) for raw in cases]

    assert run_page_function("nameAsTyped", calls) == [typed for typed, _ in cases.values()]
    assert run_page_function("nameFinal", calls) == [final for _, final in cases.values()]


def test_email_ok_needs_one_at_no_spaces_and_a_dot_in_the_domain():
    good = ["ada@example.com", "a@b.co", "x" * 242 + "@example.com"]
    bad = [
        "ada.example.com",
        "ada @example.com",
        "ada@example",
        "a@@b.co",
        "",
        "x" * 243 + "@example.com",
    ]

    assert run_page_function("emailOk", [json.dumps(v) for v in good]) == [True] * len(good)
    assert run_page_function("emailOk", [json.dumps(v) for v in bad]) == [False] * len(bad)


def compose(**changes):
    return run_page(f"composed(Object.assign({{}}, DEFAULTS, {json.dumps(changes)}))")


def test_composed_matches_the_prototype_with_gics_sector_names():
    assert run_page("composed(DEFAULTS)") == (
        "Each morning, look at Information Technology stocks and pick the one with the strongest "
        "5-day gain. Buy about $2,500 of it. Hold each position for about 3 days, then sell. Sell "
        "a position if it falls 4% below what you paid. Keep at least 25% in cash. One order a day."
    )
    assert compose(aggr=5, hold=1, stop=0, signal="dip", sectors=["Energy", "Healthcare"]) == (
        "Each morning, look at Energy, Health Care stocks and pick the one with the biggest "
        "one-day drop (buy the dip). Buy about $5,000 of it. Hold each position for about 1 day, "
        "then sell. Stay fully invested. One order a day."
    )
    assert compose(aggr=1, hold=10, stop=10, signal="news", sectors=[]) == (
        "Each morning, look at any S&P 500 stock and pick the one with the most positive news "
        "that day. Buy about $1,000 of it. Hold each position for about 10 days, then sell. Sell "
        "a position if it falls 10% below what you paid. Keep at least 60% in cash. One order a "
        "day."
    )
    assert compose(aggr=4, sectors=["Consumer", "Tech", "Financials", "Industrials"]) == (
        "Each morning, look at Consumer Discretionary or Consumer Staples, Information "
        "Technology, Financials, Industrials stocks and pick the one with the strongest 5-day "
        "gain. Buy about $3,500 of it. Hold each position for about 3 days, then sell. Sell a "
        "position if it falls 4% below what you paid. Keep at least 10% in cash. One order a day."
    )
    assert compose(aggr=2, stop=2) == (
        "Each morning, look at Information Technology stocks and pick the one with the strongest "
        "5-day gain. Buy about $1,500 of it. Hold each position for about 3 days, then sell. Sell "
        "a position if it falls 2% below what you paid. Keep at least 40% in cash. One order a day."
    )


GICS_SECTORS = (
    "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy",
    "Financials", "Health Care", "Industrials", "Information Technology", "Materials",
    "Real Estate", "Utilities",
)  # fmt: skip


def test_chip_labels_stay_the_prototypes_and_map_to_real_gics_sectors():
    where = run_page("SECTOR_WHERE")

    assert run_page("SECTORS") == [
        "Tech",
        "Energy",
        "Healthcare",
        "Financials",
        "Consumer",
        "Industrials",
    ]
    assert sorted(where) == sorted(run_page("SECTORS"))
    for label, phrase in where.items():
        assert set(phrase.split(" or ")) <= set(GICS_SECTORS), (label, phrase)
    assert run_page("SIGNALS") == [
        ["momentum", "Momentum"],
        ["dip", "Buy the dip"],
        ["news", "News"],
    ]
    assert run_page("AGGRESSIVENESS") == ["Very cautious", "Cautious", "Balanced", "Bold", "All in"]
    assert run_page("DEFAULTS") == {
        "aggr": 3, "hold": 3, "stop": 4, "signal": "momentum", "sectors": ["Tech"], "custom": None
    }  # fmt: skip


def test_controls_rewrite_the_text_and_typing_keeps_it():
    steps = run_page(
        """(function () {
          var s = next(DEFAULTS, {type: "text", value: "my own words, at least twenty"});
          var typed = strategyText(s);
          var slid = next(s, {type: "set", key: "hold", value: "1"});
          var signal = next(next(DEFAULTS, {type: "signal", id: "news"}), {type: "signal", id: "dip"});
          var a = next(DEFAULTS, {type: "sector", label: "Energy"});
          var b = next(a, {type: "sector", label: "Tech"});
          var order = next(next(DEFAULTS, {type: "sector", label: "Industrials"}),
                           {type: "sector", label: "Energy"});
          var c = next(next(DEFAULTS, {type: "text", value: "x"}), {type: "sector", label: "Tech"});
          return {typed: typed, slid: [slid.hold, slid.custom, strategyText(slid) === composed(slid)],
                  signal: signal.signal, a: a.sectors, b: b.sectors, c: [c.sectors, c.custom],
                  order: order.sectors,
                  unchanged: DEFAULTS.sectors};
        })()"""
    )

    assert steps["typed"] == "my own words, at least twenty"
    assert steps["slid"] == [1, None, True]  # a slider value is a number; the text is rewritten
    assert steps["signal"] == "dip"  # single-select
    assert steps["a"] == ["Tech", "Energy"]  # toggled on, in toggle order
    assert steps["b"] == ["Energy"]  # toggled off
    assert steps["order"] == [
        "Tech",
        "Industrials",
        "Energy",
    ]  # the prototype's order, not the chips'
    assert steps["c"] == [[], None]  # any control drops custom text
    assert steps["unchanged"] == ["Tech"]  # the reducer never mutates its input


def test_chips_are_buttons_with_aria_pressed():
    html = page()

    assert re.search(r'button\.type = "button";', html)
    assert 'setAttribute("aria-pressed", String(' in html
    assert re.search(r"min-height: 44px", re.search(r"\.chip \{[^}]*\}", html).group(0))


def test_local_storage_keeps_only_id_name_and_position():
    html = page()
    saved = re.findall(r"var saved = \{([^}]*)\}", html)
    store = re.search(r"var store = \{(.*?)\n  \};", html, re.DOTALL).group(1)

    assert saved, "the page should save its submission"
    for literal in saved:
        assert re.findall(r"(\w+):", literal) == ["id", "name", "position"]
    assert re.findall(r"store\.set\(([^)]*)\)", html) == ["saved"]
    assert (
        html.count("localStorage")
        == html.count("window.localStorage")
        == store.count("localStorage")
    )
    assert not re.search(r"email|company|role", store)
    assert "console." not in html


BRAND = PAGE.parent / "brand"
# Vendored unmodified from github.com/pydantic/brand @ 83c4f63 (see static/brand/README.md).
LOGOS = {
    "pydantic-name-dark.svg": (
        "Pydantic",
        "339cd97bcccf390c79674180dbdc325aa376cbdda12f473503e3f3cc84166701",
    ),
}


def test_the_vendored_logos_are_byte_identical_to_the_brand_repo_files():
    for name, (_, digest) in LOGOS.items():
        assert hashlib.sha256((BRAND / name).read_bytes()).hexdigest() == digest, name
        assert digest in (BRAND / "README.md").read_text(encoding="utf-8"), name


def test_the_page_shows_the_pydantic_logo_same_origin_with_alt_text_and_a_reserved_size():
    images = re.findall(r"<img\b[^>]*>", page())

    for name, (alt, _) in LOGOS.items():
        tag = next(t for t in images if f'src="/static/brand/{name}"' in t)
        viewbox = re.search(r'viewBox="0 0 (\d+) (\d+)"', (BRAND / name).read_text()).groups()
        assert f'alt="{alt}"' in tag
        assert f'width="{viewbox[0]}" height="{viewbox[1]}"' in tag  # the logo's own ratio
        assert 'decoding="async"' in tag
    brand = re.findall(r'src="/static/brand/([\w.-]+)"', page())
    assert brand == ["pydantic-name-dark.svg"], brand  # only the Pydantic logo (B3b)
    assert all(re.search(r'src="/static/brand/[\w.-]+\.svg"', tag) for tag in images), images


def test_the_logfire_logo_is_no_longer_vendored():
    assert sorted(path.name for path in BRAND.iterdir()) == ["README.md", "pydantic-name-dark.svg"]
    assert "pydantic-logfire" not in (BRAND / "README.md").read_text(encoding="utf-8")

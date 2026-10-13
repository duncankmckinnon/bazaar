"""The board in a real browser: its in-sample disclosure is visible, on one line, at every screen
it is shown on. Needs Playwright and its Chromium:

    uv run --with playwright==1.63.0 python -m pytest services/web/tests/test_browser_board.py

Without them (plain `uv run pytest`) every test here SKIPS with a named reason; with
BAZAAR_REQUIRE_BROWSER_PROOF=1 a missing browser FAILS instead. Offline: every request that
isn't the app's own is aborted (the fonts fall back, so the check holds with either).
"""

import asyncio
import os
import socket
import threading

import pytest
import uvicorn
from bazaar_web.app import create_app

try:
    from playwright import sync_api
except ImportError:
    sync_api = None


def browser_unavailable() -> str | None:
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
    UNAVAILABLE is not None and not REQUIRED,
    reason=f"run with: uv run --with playwright==1.63.0 python -m pytest {__file__}: {UNAVAILABLE}",
)

# Anthony's TV (the right half of a split screen), a narrower split, his laptop window, the
# projector.
SCREENS = [(1110, 1045), (1000, 1045), (960, 966), (1920, 1080)]


@pytest.fixture
def board_url(settings, helpers):
    if UNAVAILABLE is not None:
        pytest.fail(f"BAZAAR_REQUIRE_BROWSER_PROOF=1 but no browser: {UNAVAILABLE}", pytrace=False)
    app = create_app(settings, helpers.FakeRunner())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    helpers.wait_for(lambda: server.started, timeout=10)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.parametrize(("width", "height"), SCREENS)
def test_the_in_sample_disclosure_is_visible_on_one_line(board_url, width, height):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route(
            "**/*", lambda r: r.continue_() if r.request.url.startswith(board_url) else r.abort()
        )
        page.goto(f"{board_url}/")
        page.wait_for_function("!document.getElementById('connecting')")
        note = page.evaluate(
            """() => {
              const p = document.querySelector('.ar-note'), box = p.getBoundingClientRect();
              const shown = [...p.childNodes].filter(n => n.nodeType === 3 || getComputedStyle(n).display !== 'none');
              return {
                text: shown.map(n => n.textContent).join('').trim(),
                visible: !p.hidden && getComputedStyle(p).display !== 'none' && box.height > 0 && box.bottom <= innerHeight,
                oneLine: p.scrollWidth <= p.clientWidth + 1 && box.height < 2 * parseFloat(getComputedStyle(p).lineHeight),
                fontSize: parseFloat(getComputedStyle(p).fontSize),
              };
            }"""
        )
        browser.close()

    assert note["visible"] and note["oneLine"], note
    assert note["text"].startswith("AI") and "in-sample" in note["text"], note
    assert note["fontSize"] >= 12


@pytest.fixture
def held_board(settings, helpers):
    """The app with 16 scored runs, its /api/board held until release() (CONNECTING till then)."""
    if UNAVAILABLE is not None:
        pytest.fail(f"BAZAAR_REQUIRE_BROWSER_PROOF=1 but no browser: {UNAVAILABLE}", pytrace=False)
    for i in range(16):
        ret = 0.05 - i * 0.002
        marks = tuple(str(round(10000 * (1 + ret * d / 10), 2)) for d in range(1, 11))
        helpers.write_run(
            settings.runs_dir, f"run-{i}", policy_ref=f"agent-{i}", period_return=str(ret),
            marks=marks,
        )  # fmt: skip
    gate = threading.Event()
    app = create_app(settings, helpers.FakeRunner())

    @app.middleware("http")
    async def hold(request, call_next):
        if request.url.path == "/api/board":
            await asyncio.to_thread(gate.wait, 30)
        return await call_next(request)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    helpers.wait_for(lambda: server.started, timeout=10)
    yield f"http://127.0.0.1:{port}", gate.set
    gate.set()
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.parametrize(("width", "height"), SCREENS)
def test_the_qr_is_full_size_before_the_board_data_arrives(held_board, width, height):
    # Live, the first load after a deploy showed a 77 px QR until the data came: the bottom row
    # (the QR's height) was as short as the empty list. The list now reserves the top 15's room.
    url, release = held_board
    measure = """() => {
      const q = document.querySelector('.ar-qr').getBoundingClientRect();
      return {qr: q.width, bottom: document.querySelector('.bottom').getBoundingClientRect().height,
              rows: document.querySelectorAll('#list .ar-row').length,
              connecting: !!document.getElementById('connecting')};
    }"""
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(url) else r.abort())
        page.goto(f"{url}/")
        page.wait_for_timeout(500)  # a few frames with the data held
        held = page.evaluate(measure)
        release()
        page.wait_for_function("document.querySelectorAll('#list .ar-row').length === 15")
        page.wait_for_timeout(300)
        settled = page.evaluate(measure)
        browser.close()

    assert held["connecting"] and held["rows"] == 0  # measured before any data
    assert held["qr"] >= 0.95 * settled["qr"], (held, settled)
    assert abs(held["bottom"] - settled["bottom"]) <= 1, (held, settled)  # nothing moves

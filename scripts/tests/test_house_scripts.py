import importlib.util
import itertools
import json
import re
from pathlib import Path

import httpx
import pytest
from bazaar_web.store import OPERATOR_CONTACT_EMAIL

SCRIPTS = Path(__file__).parents[1]
UNIVERSE = {"AAPL", "AMZN", "EA", "FISV", "JNJ", "JPM", "KO", "META", "MSFT", "NVDA", "WMT", "XOM"}
SLUG = re.compile(r"^[a-z0-9-]{3,40}$")
BASE = "https://bazaar.example"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


house_strategies = load("house_strategies")
house_feeder = load("house_feeder")


def tickers(text):
    return set(re.findall(r"\b[A-Z]{1,5}\b", text))


SENTINEL = "tok-SENTINEL-123"
HEADER = "x-bazaar-admin-token"


@pytest.fixture(autouse=True)
def admin_token(monkeypatch, capsys):
    """Every test runs with a sentinel token, and the token must never be printed."""
    monkeypatch.setenv("BAZAAR_ADMIN_TOKEN", SENTINEL)
    yield
    out, err = capsys.readouterr()
    assert SENTINEL not in out and SENTINEL not in err


def output(capsys):
    out, err = capsys.readouterr()
    assert SENTINEL not in out and SENTINEL not in err
    return out, err


def reserved_handle_422():
    detail = [{"type": "value_error", "loc": ["body", "handle"], "msg": "That handle is reserved"}]
    return httpx.Response(422, json={"detail": detail})


class Sleeps(list):
    """Records every sleep instead of waiting."""

    def __call__(self, seconds):
        self.append(seconds)


# ---------- house_strategies ----------


def test_house_batch_is_30_valid_strategies_on_the_12_stocks():
    strategies = house_strategies.STRATEGIES

    assert len(strategies) == 30
    for name, text in strategies.items():
        assert SLUG.match(name), name
        assert 20 <= len(text) <= 4000, name
        assert tickers(text) <= UNIVERSE, (name, tickers(text) - UNIVERSE)


def batch_server(responses):
    """POST answers come from `responses` in order; the last one repeats."""
    posts = []

    def handler(request):
        posts.append(json.loads(request.content))
        return responses[min(len(posts) - 1, len(responses) - 1)]

    return httpx.Client(transport=httpx.MockTransport(handler)), posts


@pytest.mark.parametrize(
    "taken",
    [
        httpx.Response(409, json={"detail": "That name is taken."}),
        httpx.Response(422, json={"detail": "that name is taken"}),  # what the web app sends
    ],
)
def test_house_batch_skips_a_taken_name_without_a_retry_wait(capsys, taken):
    ok = httpx.Response(201, json={"id": "s2", "status": "queued", "position": 1})
    client, posts = batch_server([taken, ok])
    sleeps = Sleeps()

    assert house_strategies.submit_all(client, BASE, only=2, sleep=sleeps) == 1
    assert [p["name"] for p in posts] == list(house_strategies.STRATEGIES)[:2]
    assert sleeps == [2.0, 2.0]  # only the pacing between submissions, no 60s retry wait
    assert "skip blue-chip-hodl: name taken" in output(capsys)[0]


def test_house_batch_waits_60s_on_429_and_retries_the_same_name():
    limited = httpx.Response(429, json={"detail": "Too many submissions."})
    ok = httpx.Response(201, json={"id": "s1", "status": "queued", "position": 1})
    client, posts = batch_server([limited, ok])
    sleeps = Sleeps()

    assert house_strategies.submit_all(client, BASE + "/", only=1, sleep=sleeps) == 1
    assert [p["name"] for p in posts] == ["blue-chip-hodl", "blue-chip-hodl"]
    assert posts[0]["handle"] == "house"
    assert sleeps == [60.0, 2.0]


# ---------- house_feeder ----------


class Board:
    """A fake service: POSTs add a queued row, and each sleep finishes one busy row."""

    def __init__(self, busy=0, taken=(), statuses=None):
        self.busy, self.taken, self.statuses = busy, set(taken), list(statuses or [])
        self.posted, self.depth_at_post = [], []

    def handler(self, request):
        if request.url.path == "/api/board":
            rows = [{"id": str(i), "status": "running"} for i in range(self.busy)]
            rows.append({"id": "done", "status": "scored"})
            return httpx.Response(200, json={"rows": rows})
        body = json.loads(request.content)
        self.posted.append(body)
        self.depth_at_post.append(self.busy)
        if body["name"] in self.taken:
            return httpx.Response(409, json={"detail": "name is taken"})
        if self.statuses:
            return self.statuses.pop(0)
        self.busy += 1
        return httpx.Response(201, json={"id": body["name"], "status": "queued", "position": 1})

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.busy = max(0, self.busy - 1)

    def run(self, **kwargs):
        self.sleeps = []
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return house_feeder.feed(client, BASE, sleep=self.sleep, **kwargs)


def test_feeder_never_posts_while_depth_or_more_are_busy():
    board = Board(busy=6)

    assert board.run(depth=4, limit=12) == 12
    assert len(board.posted) == 12
    assert max(board.depth_at_post) < 4
    assert 10.0 in board.sleeps  # it waited for the board to drain


def test_feeder_stops_at_max(capsys):
    board = Board()

    assert board.run(depth=100, limit=5) == 5
    assert len(board.posted) == 5
    assert output(capsys)[0].rstrip().endswith("done")


def test_feeder_skips_a_taken_name_instantly():
    first = next(house_feeder.strategies())[0]
    board = Board(taken={first})

    assert board.run(depth=100, limit=1) == 1
    assert len(board.posted) == 2
    assert board.posted[0]["name"] == first
    assert board.sleeps == [3.0]  # no sleep at all for the skip, only pacing after the success


def test_feeder_skips_the_web_apps_422_that_name_is_taken_instantly():
    board = Board(statuses=[httpx.Response(422, json={"detail": "that name is taken"})])

    assert board.run(depth=100, limit=1) == 1
    assert board.sleeps == [3.0]


def test_feeder_waits_5_minutes_on_429_and_30s_on_other_errors():
    board = Board(
        statuses=[httpx.Response(429, text="slow down"), httpx.Response(500, text="oops")]
    )

    assert board.run(depth=100, limit=1) == 1
    assert board.sleeps == [300.0, 3.0, 30.0, 3.0, 3.0]
    assert board.posted[0]["handle"] == "house"


def test_generated_names_are_valid_unique_and_deterministic():
    first = list(itertools.islice(house_feeder.strategies(), 200))
    second = list(itertools.islice(house_feeder.strategies(), 200))
    names = [name for name, _ in first]

    assert first == second
    assert len(set(names)) == len(names)
    for name, text in first:
        assert SLUG.match(name), name
        assert 20 <= len(text) <= 4000
        assert tickers(text) <= UNIVERSE, name


# ---------- the reserved house handle ----------


def recording_client(responses):
    """Every request is recorded; POSTs answer from `responses`, the board is empty."""
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/board":
            return httpx.Response(200, json={"rows": []})
        posts = sum(r.method == "POST" for r in requests)
        return responses[min(posts - 1, len(responses) - 1)]

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


OK = httpx.Response(201, json={"id": "s1", "status": "queued", "position": 1})


def test_token_goes_on_posts_only(capsys):
    client, requests = recording_client([OK])

    assert house_feeder.feed(client, BASE, depth=100, limit=1, sleep=Sleeps()) == 1
    assert house_strategies.submit_all(client, BASE, only=1, sleep=Sleeps()) == 1
    posts = [r for r in requests if r.method == "POST"]
    gets = [r for r in requests if r.method == "GET"]
    assert len(posts) == 2 and gets
    assert all(r.headers.get(HEADER) == SENTINEL for r in posts)
    assert all(HEADER not in r.headers for r in gets)
    output(capsys)


@pytest.mark.parametrize("handle", ["house", "HOUSE", " house "])
def test_house_handle_without_a_token_stops_before_any_request(monkeypatch, capsys, handle):
    monkeypatch.delenv("BAZAAR_ADMIN_TOKEN")
    for run in (
        lambda c: house_strategies.submit_all(c, BASE, handle=handle, sleep=Sleeps()),
        lambda c: house_feeder.feed(c, BASE, handle=handle, sleep=Sleeps()),
    ):
        client, requests = recording_client([OK])
        with pytest.raises(SystemExit) as stopped:
            run(client)
        assert stopped.value.code != 0
        assert requests == []
    _, err = output(capsys)
    assert err.count("BAZAAR_ADMIN_TOKEN is required to submit as the house handle") == 2


def test_empty_token_counts_as_missing(monkeypatch):
    monkeypatch.setenv("BAZAAR_ADMIN_TOKEN", "")
    client, requests = recording_client([OK])
    with pytest.raises(SystemExit):
        house_strategies.submit_all(client, BASE, sleep=Sleeps())
    assert requests == []


def test_other_handles_work_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("BAZAAR_ADMIN_TOKEN")
    client, requests = recording_client([OK])

    assert house_strategies.submit_all(client, BASE, handle="crew", only=1, sleep=Sleeps()) == 1
    assert house_feeder.feed(client, BASE, handle="crew", depth=100, limit=1, sleep=Sleeps()) == 1
    assert all(HEADER not in r.headers for r in requests)
    output(capsys)


def test_reserved_handle_422_is_fatal_in_both_scripts_after_one_post(capsys):
    for run in (
        lambda c, s: house_strategies.submit_all(c, BASE, only=3, sleep=s),
        lambda c, s: house_feeder.feed(c, BASE, depth=100, limit=3, sleep=s),
    ):
        client, requests = recording_client([reserved_handle_422()])
        sleeps = Sleeps()
        with pytest.raises(SystemExit) as stopped:
            run(client, sleeps)
        assert stopped.value.code != 0
        assert sum(r.method == "POST" for r in requests) == 1
        assert sleeps == []
    out, _ = output(capsys)
    assert out.count("That handle is reserved") == 2


def test_feeder_stops_on_any_other_422_without_looping(capsys):
    too_long = httpx.Response(
        422,
        json={"detail": [{"loc": ["body", "instructions"], "msg": "String too long"}]},
    )
    client, requests = recording_client([too_long])
    sleeps = Sleeps()

    with pytest.raises(SystemExit) as stopped:
        house_feeder.feed(client, BASE, depth=100, limit=5, sleep=sleeps)
    assert stopped.value.code != 0
    assert sum(r.method == "POST" for r in requests) == 1
    assert sleeps == []
    assert "String too long" in output(capsys)[0]


def test_batch_keeps_going_past_another_422(capsys):
    too_long = httpx.Response(
        422,
        json={"detail": [{"loc": ["body", "instructions"], "msg": "String too long"}]},
    )
    client, requests = recording_client([too_long, OK])

    assert house_strategies.submit_all(client, BASE, only=2, sleep=Sleeps()) == 1
    assert sum(r.method == "POST" for r in requests) == 2
    assert "FAIL blue-chip-hodl: 422" in output(capsys)[0]


def test_name_taken_422_still_skips_instantly_in_the_feeder():
    taken = httpx.Response(422, json={"detail": "that name is taken"})
    client, _ = recording_client([taken, OK])
    sleeps = Sleeps()

    assert house_feeder.feed(client, BASE, depth=100, limit=1, sleep=sleeps) == 1
    assert sleeps == [3.0]  # only the pacing after the success


def test_a_house_post_carries_the_operator_contact_and_the_admin_header():
    client, requests = recording_client([OK])

    assert house_strategies.submit_all(client, BASE, only=1, sleep=Sleeps()) == 1
    assert house_feeder.feed(client, BASE, depth=100, limit=1, sleep=Sleeps()) == 1
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 2
    for post in posts:
        body = json.loads(post.content)
        assert set(body) == {"name", "handle", "instructions", "email", "company", "role"}
        assert body["handle"] == "house"
        assert {k: body[k] for k in ("email", "company", "role")} == house_feeder.OPERATOR_CONTACT
        assert post.headers.get(HEADER) == SENTINEL
    assert house_strategies.OPERATOR_CONTACT == house_feeder.OPERATOR_CONTACT
    # The contacts CSV leaves house runs out by this exact email.
    assert house_feeder.OPERATOR_CONTACT["email"] == OPERATOR_CONTACT_EMAIL

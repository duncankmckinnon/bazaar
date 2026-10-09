import importlib.util
import itertools
import json
import re
from pathlib import Path

import httpx
import pytest

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
    assert "skip blue-chip-hodl: name taken" in capsys.readouterr().out


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
    assert capsys.readouterr().out.rstrip().endswith("done")


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

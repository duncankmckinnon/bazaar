import httpx
import pytest
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.http import get


def flaky(failures, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) <= failures:
            raise httpx.ReadError("Connection reset by peer", request=request)
        return httpx.Response(200, text="ok")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_dropped_connection_is_retried_and_the_response_returned():
    seen, naps = [], []

    response = get(flaky(2, seen), "https://example.test/a", sleep=naps.append)

    assert response.text == "ok"
    assert len(seen) == 3
    assert naps == [1.0, 2.0]  # the wait doubles between attempts


def test_a_connection_that_keeps_dropping_stops_the_fetch_after_three_attempts():
    seen = []

    with pytest.raises(SourceError, match=r"example\.test"):
        get(flaky(99, seen), "https://example.test/a", sleep=lambda _: None)

    assert len(seen) == 3


def test_a_refusal_is_returned_to_the_caller_without_retrying():
    seen = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(403))
    )

    response = get(client, "https://example.test/a", sleep=lambda _: None)

    assert (response.status_code, len(seen)) == (403, 1)

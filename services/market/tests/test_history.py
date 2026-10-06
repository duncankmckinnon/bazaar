from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bazaar_market.db import MarketError
from bazaar_market.history import PageScope, build_page, parse_history_request
from bazaar_protocol import ErrorCode
from bazaar_protocol.research import HistoryPage, HistoryRequest

T0 = datetime(2025, 7, 1, 20, 0, tzinfo=UTC)
SECRET = b"test-secret"
SCOPE = PageScope(
    experiment_id=uuid4(),
    account_id=uuid4(),
    agent_id=uuid4(),
    strategy_version_id=uuid4(),
    cutoff_at=T0 + timedelta(days=10),
    data_version="test-v1",
)
StrPage = HistoryPage[str]
# Two items share a timestamp; order breaks the tie on the key.
ITEMS = [
    ((T0 + timedelta(days=d), k), f"{d}{k}") for d, k in [(3, "b"), (0, "z"), (3, "a"), (5, "c")]
]


def request(**changes) -> HistoryRequest:
    fields = {"start_at": T0, "end_at": T0 + timedelta(days=10), "limit": 100, **changes}
    return HistoryRequest(**fields)


def page(req: HistoryRequest, scope: PageScope = SCOPE, secret: bytes = SECRET, route="orders"):
    return build_page(StrPage, scope, req, route=route, source="s", items=ITEMS, secret=secret)


def test_envelope_echoes_the_scope_and_window_in_key_order():
    req = request(start_at=T0 + timedelta(days=1))
    result = page(req)
    assert result.items == ("3a", "3b", "5c")
    assert (result.start_at, result.end_at, result.cutoff_at) == (
        req.start_at,
        req.end_at,
        SCOPE.cutoff_at,
    )
    assert (result.experiment_id, result.account_id, result.data_version) == (
        SCOPE.experiment_id,
        SCOPE.account_id,
        "test-v1",
    )
    assert (result.coverage, result.next_cursor) == ("complete", None)


def test_a_window_past_the_cutoff_is_forbidden():
    with pytest.raises(MarketError) as error:
        page(request(end_at=SCOPE.cutoff_at + timedelta(microseconds=1)))
    assert (error.value.status_code, error.value.code) == (403, ErrorCode.FORBIDDEN)
    assert page(request(end_at=SCOPE.cutoff_at)).items


def test_pages_walk_the_items_once_and_the_same_query_gives_the_same_cursor():
    first = page(request(limit=2))
    assert first.items == ("0z", "3a")
    assert page(request(limit=2)).next_cursor == first.next_cursor
    second = page(request(limit=2, cursor=first.next_cursor))
    assert second.items == ("3b", "5c")
    assert second.next_cursor is None


@pytest.mark.parametrize(
    "change",
    [
        {"limit": 3},
        {"start_at": T0 + timedelta(seconds=1)},
        {"end_at": T0 + timedelta(days=9)},
    ],
)
def test_a_cursor_only_works_for_the_query_that_issued_it(change):
    cursor = page(request(limit=2)).next_cursor
    with pytest.raises(MarketError) as error:
        page(request(**{"limit": 2, **change, "cursor": cursor}))
    assert error.value.status_code == 422


def test_a_cursor_is_bound_to_scope_route_cutoff_version_and_secret():
    cursor = page(request(limit=2)).next_cursor
    continuation = request(limit=2, cursor=cursor)
    for scope in (
        replace(SCOPE, account_id=uuid4()),
        replace(SCOPE, cutoff_at=SCOPE.cutoff_at + timedelta(days=1)),
        replace(SCOPE, data_version="test-v2"),
    ):
        with pytest.raises(MarketError):
            page(continuation, scope=scope)
    with pytest.raises(MarketError):
        page(continuation, route="history")
    with pytest.raises(MarketError):
        page(continuation, secret=b"other-secret")


@pytest.mark.parametrize("cursor", ["garbage", "x.y", "e30.00"])
def test_a_tampered_cursor_is_422(cursor):
    with pytest.raises(MarketError) as error:
        page(request(cursor=cursor))
    assert error.value.status_code == 422


def test_an_edited_cursor_payload_fails_the_signature():
    cursor = page(request(limit=1)).next_cursor
    token, signature = cursor.split(".")
    edited = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(MarketError):
        page(request(limit=1, cursor=f"{edited}.{signature}"))


def test_query_parsing_errors_are_422():
    with pytest.raises(MarketError) as error:
        parse_history_request("2025-07-02T00:00:00Z", "2025-07-01T00:00:00Z")
    assert error.value.status_code == 422
    assert parse_history_request("2025-07-01T00:00:00Z", "2025-07-02T00:00:00Z", "5").limit == 5


def test_a_non_ascii_cursor_is_422_not_500():
    cursor = page(request(limit=1)).next_cursor
    token, _ = cursor.split(".")
    for bad in (f"{token}.é", "é.abc"):
        with pytest.raises(MarketError) as error:
            page(request(limit=1, cursor=bad))
        assert error.value.status_code == 422

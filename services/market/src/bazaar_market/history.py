"""Paged history responses for the research routes: envelope, window, order and cursor.

A page lists the items whose timestamp is inside [start_at, end_at], ordered by (timestamp, key),
at most `limit` of them. When more remain, `next_cursor` is an opaque, HMAC-signed token bound to
the route, scope, symbol, window, limit, cutoff, data version and the last key returned. The same
query always yields the same cursor. A tampered cursor, or one presented with any other query,
is 422. A window that ends after the cutoff is 403, like the price route.

News and filings use the same builder with (published_at, record_id) keys.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Literal
from uuid import UUID

from bazaar_protocol import ErrorCode
from bazaar_protocol.research import HistoryPage, HistoryRequest
from pydantic import ValidationError

from bazaar_market.db import MarketError

Key = tuple[datetime, str]


@dataclass(frozen=True)
class PageScope:
    """Who a page is for and what it may show. Built by the server, never from the request."""

    experiment_id: UUID
    account_id: UUID
    agent_id: UUID
    strategy_version_id: UUID
    cutoff_at: datetime
    data_version: str


@cache
def cursor_secret() -> bytes:
    """BAZAAR_CURSOR_SECRET, or a random secret for this process (cursors then die on restart)."""
    configured = os.getenv("BAZAAR_CURSOR_SECRET")
    return configured.encode() if configured else secrets.token_bytes(32)


def parse_history_request(
    start_at: str | None, end_at: str | None, limit: str = "100", cursor: str | None = None
) -> HistoryRequest:
    try:
        return HistoryRequest(start_at=start_at, end_at=end_at, limit=limit, cursor=cursor)
    except ValidationError as error:
        raise MarketError(422, ErrorCode.INVALID_REQUEST, str(error.errors()[0]["msg"])) from None


def _invalid_cursor() -> MarketError:
    return MarketError(422, ErrorCode.INVALID_REQUEST, "Invalid cursor")


def _sign(payload: bytes, secret: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def encode_cursor(binding: dict[str, object], after: Key, secret: bytes) -> str:
    body = {**binding, "after": [after[0].isoformat(), after[1]]}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{token}.{_sign(payload, secret)}"


def decode_cursor(cursor: str, binding: dict[str, object], secret: bytes) -> Key:
    token, _, signature = cursor.partition(".")
    try:
        payload = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        raise _invalid_cursor() from None
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        raise _invalid_cursor()
    body = json.loads(payload)
    after = body.pop("after")
    if body != binding:
        raise _invalid_cursor()
    return datetime.fromisoformat(after[0]), after[1]


def build_page[T](
    page_type: type[HistoryPage[T]],
    scope: PageScope,
    request: HistoryRequest,
    *,
    route: str,
    source: str,
    items: Iterable[tuple[Key, T]],
    symbol: str | None = None,
    coverage: Literal["complete", "missing", "partial"] = "complete",
    secret: bytes | None = None,
) -> HistoryPage[T]:
    """One page of `items`, each given with its (timestamp, key). Keys must be unique."""
    if request.end_at > scope.cutoff_at:
        raise MarketError(403, ErrorCode.FORBIDDEN, "end_at is after the experiment's current time")
    secret = secret or cursor_secret()
    binding: dict[str, object] = {
        "route": route,
        "experiment_id": str(scope.experiment_id),
        "account_id": str(scope.account_id),
        "symbol": symbol,
        "start_at": request.start_at.isoformat(),
        "end_at": request.end_at.isoformat(),
        "limit": request.limit,
        "cutoff_at": scope.cutoff_at.isoformat(),
        "data_version": scope.data_version,
    }
    rows = sorted(
        ((key, item) for key, item in items if request.start_at <= key[0] <= request.end_at),
        key=lambda row: row[0],
    )
    if len({key for key, _ in rows}) != len(rows):
        raise MarketError(500, ErrorCode.INTERNAL_ERROR, "History keys are not unique")
    if request.cursor is not None:
        after = decode_cursor(request.cursor, binding, secret)
        rows = [row for row in rows if row[0] > after]
    page, rest = rows[: request.limit], rows[request.limit :]
    return page_type(
        experiment_id=scope.experiment_id,
        account_id=scope.account_id,
        agent_id=scope.agent_id,
        strategy_version_id=scope.strategy_version_id,
        cutoff_at=scope.cutoff_at,
        start_at=request.start_at,
        end_at=request.end_at,
        source=source,
        data_version=scope.data_version,
        coverage=coverage,
        items=tuple(item for _, item in page),
        next_cursor=encode_cursor(binding, page[-1][0], secret) if rest else None,
    )

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from .errors import SourceError

ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_PAUSE = 60.0


def _pause(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before the next attempt: the server's Retry-After, or 1 s then 2 s."""
    if response is not None:
        try:
            return min(float(response.headers["Retry-After"]), MAX_PAUSE)
        except (KeyError, ValueError):
            pass
    return float(2 ** (attempt - 1))


def get(
    http: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET with up to three attempts for dropped connections and 429, 502, 503 or 504.

    Redirects are never followed, so request headers that carry keys stay with the host they
    were sent to. Other status codes are returned for the caller to judge.
    """
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = http.get(url, params=params, headers=headers, follow_redirects=False)
        except httpx.TransportError as exc:
            if attempt == ATTEMPTS:
                raise SourceError(f"{url} failed {ATTEMPTS} times: {exc}") from exc
            sleep(_pause(None, attempt))
            continue
        if response.status_code in RETRY_STATUSES and attempt < ATTEMPTS:
            sleep(_pause(response, attempt))
            continue
        return response
    raise AssertionError("unreachable")

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from .errors import SourceError

ATTEMPTS = 3


def get(
    http: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET that retries dropped connections. Status codes are left for the caller to judge."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return http.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            if attempt == ATTEMPTS:
                raise SourceError(f"{url} failed {ATTEMPTS} times: {exc}") from exc
            sleep(float(2 ** (attempt - 1)))
    raise AssertionError("unreachable")

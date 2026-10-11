from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from bazaar_runner.bundle import (
    BUNDLE_ENV,
    DEMO_BUNDLE,
    BundleError,
    BundleInfo,
    bundle_from,
    check_bundle_id,
    exchange_days,
    read_bundle,
    run_bundle_id,
)
from bazaar_runner.clock import build_schedule, schedule_digest
from bazaar_runner.http_market import APPROVAL_HEADER, RUNNER_TOKEN_HEADER

from .market_fakes import SYMBOLS, TOKEN, InMemoryMarket, bundle_body, delegating_transport

V1 = (date(2026, 2, 2), date(2026, 2, 13))
V2 = (date(2026, 9, 28), date(2026, 10, 9))


def body(bundle_id: str, start: date, end: date, **kwargs) -> dict:
    """A well-formed body whose sessions are the exchange days of its window."""
    kwargs.setdefault("sessions", exchange_days(start, end))
    kwargs.setdefault("expected", len(exchange_days(start, end)))
    return bundle_body(bundle_id, start=start, end=end, **kwargs)


def check(payload: dict):
    return bundle_from(BundleInfo.model_validate(payload))


def market(*bodies: dict) -> tuple[httpx.AsyncClient, InMemoryMarket]:
    fake = InMemoryMarket()
    fake.bundles = {b["bundle_id"]: b for b in bodies}
    client = httpx.AsyncClient(transport=delegating_transport(fake), base_url="http://m")
    return client, fake


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


async def test_v2_is_ten_edt_sessions_from_the_market():
    client, fake = market(body(DEMO_BUNDLE, *V2))
    bundle = await read_bundle(client, DEMO_BUNDLE)

    assert bundle.bundle_id == DEMO_BUNDLE and bundle.symbols == SYMBOLS
    days = [s.date for s in bundle.sessions]
    assert len(days) == 10 and (days[0], days[-1]) == V2
    assert date(2026, 10, 3) not in days  # a Saturday
    # Daylight time: 09:30 and 16:00 New York are 13:30Z and 20:00Z.
    assert {
        (s.open_at - utc(s.date, 13, 30), s.close_at - utc(s.date, 20)) for s in bundle.sessions
    } == {(timedelta(0), timedelta(0))}
    # The route needs no credentials and the runner sends none.
    ((_, _, headers),) = [c for c in fake.calls if c[0] == "bundle"]
    assert APPROVAL_HEADER.lower() not in headers and RUNNER_TOKEN_HEADER.lower() not in headers
    assert TOKEN not in str(headers)


async def test_v1_is_still_the_ten_est_sessions():
    client, _ = market(body("demo-bundle-v1", *V1))
    bundle = await read_bundle(client, "demo-bundle-v1")

    assert [s.date.day for s in bundle.sessions] == [2, 3, 4, 5, 6, 9, 10, 11, 12, 13]
    # Standard time: 14:30Z and 21:00Z.
    assert all(
        (s.open_at, s.close_at) == (utc(s.date, 14, 30), utc(s.date, 21)) for s in bundle.sessions
    )


def test_the_same_bundle_gives_the_same_schedule():
    first, second = check(body(DEMO_BUNDLE, *V2)), check(body(DEMO_BUNDLE, *V2))
    assert first == second
    assert schedule_digest(build_schedule(first.script)) == schedule_digest(
        build_schedule(second.script)
    )
    assert schedule_digest(build_schedule(first.script)) != schedule_digest(
        build_schedule(check(body("demo-bundle-v1", *V1)).script)
    )


async def test_an_unknown_bundle_stops_the_launch():
    client, _ = market(body(DEMO_BUNDLE, *V2))
    with pytest.raises(BundleError, match="the market has no bundle demo-bundle-v9"):
        await read_bundle(client, "demo-bundle-v9")


def test_a_holiday_inside_the_window_is_skipped():
    # Labor Day, Monday 2026-09-07: the window needs an extra weekday to hold ten sessions.
    days = exchange_days(date(2026, 8, 31), date(2026, 9, 14))
    assert len(days) == 10 and date(2026, 9, 7) not in days
    sessions = check(body("labor-day", date(2026, 8, 31), date(2026, 9, 14))).sessions
    assert (sessions[4].date, sessions[5].date) == (date(2026, 9, 4), date(2026, 9, 8))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"incomplete": [(date(2026, 10, 1), ("EA",))]}, "2026-10-01 lacks EA"),
        # The market dropped a weekday it has no complete bars for: nine sessions.
        (
            {"sessions": exchange_days(*V2)[:4] + exchange_days(*V2)[5:]},
            "9 sessions where the market expects 10",
        ),
        ({"sessions": (date(2026, 9, 25), *exchange_days(*V2)[1:])}, "2026-09-25 fall outside"),
        # Labor Day is a holiday, so a market that serves it disagrees with the exchange calendar.
        (
            {
                "start": date(2026, 9, 7),
                "end": date(2026, 9, 18),
                "sessions": tuple(date(2026, 9, d) for d in (7, 8, 9, 10, 11, 14, 15, 16, 17, 18)),
            },
            "are not the exchange days",
        ),
        # The market's holiday list disagrees with the runner's (it forgot Labor Day).
        (
            {
                "start": date(2026, 8, 31),
                "end": date(2026, 9, 14),
                "expected": 11,
                "sessions": (
                    *exchange_days(date(2026, 8, 31), date(2026, 9, 14)),
                    date(2026, 9, 7),
                ),
            },
            "the market expects 11 sessions in 2026-08-31..2026-09-14 and the NYSE calendar has 10",
        ),
        # A whole missing day, as the market now reports it.
        ({"incomplete": [(date(2026, 10, 5), SYMBOLS)]}, "2026-10-05 lacks AAPL, AMZN"),
    ],
)
def test_a_bundle_the_runner_cannot_trust_fails_loudly(change, reason):
    start, end = change.pop("start", V2[0]), change.pop("end", V2[1])
    with pytest.raises(BundleError, match=reason):
        check(body(DEMO_BUNDLE, start, end, **change))


def test_a_window_in_a_year_without_a_holiday_list_fails():
    with pytest.raises(BundleError, match="no NYSE holiday list for 2027"):
        exchange_days(date(2027, 1, 4), date(2027, 1, 15))


async def test_a_malformed_or_mismatched_answer_fails():
    broken = body(DEMO_BUNDLE, *V2)
    del broken["symbols"]
    client, _ = market(broken)
    with pytest.raises(BundleError, match=r"malformed \(symbols\)"):
        await read_bundle(client, DEMO_BUNDLE)

    client, fake = market(body("demo-bundle-v1", *V1))
    fake.bundles[DEMO_BUNDLE] = fake.bundles["demo-bundle-v1"]
    with pytest.raises(BundleError, match="got demo-bundle-v1"):
        await read_bundle(client, DEMO_BUNDLE)


async def test_an_unreachable_market_fails_without_echoing_the_error():
    def down(request):
        raise httpx.ConnectError(f"refused {TOKEN}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://m")
    with pytest.raises(BundleError, match=r"\(ConnectError\)$") as caught:
        await read_bundle(client, DEMO_BUNDLE)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, DEMO_BUNDLE),
        ({BUNDLE_ENV: ""}, DEMO_BUNDLE),
        ({BUNDLE_ENV: "demo-bundle-v1"}, "demo-bundle-v1"),
    ],
)
def test_the_operator_env_picks_the_bundle(environ, expected):
    assert run_bundle_id(environ) == expected


@pytest.mark.parametrize(
    "bad", ["../market", "demo bundle", "v2?x=1", "v2/../v1", "ünï", ".", "..", "-v2"]
)
def test_a_malformed_operator_bundle_is_refused_unechoed(bad):
    with pytest.raises(BundleError, match="BAZAAR_DATA_BUNDLE is not a bundle id") as caught:
        run_bundle_id({BUNDLE_ENV: bad})
    assert bad not in str(caught.value)


@pytest.mark.parametrize("bad", [".", "..", "-v2", ".v2"])
def test_an_explicit_bundle_id_that_is_a_path_step_is_refused(bad):
    with pytest.raises(BundleError, match="starts with a letter or digit"):
        check_bundle_id(bad)
    assert check_bundle_id("demo-bundle-v2.1") == "demo-bundle-v2.1"


async def test_a_dropped_bundle_read_is_retried_once():
    fake = InMemoryMarket()
    fake.bundles = {DEMO_BUNDLE: body(DEMO_BUNDLE, *V2)}
    served = delegating_transport(fake)
    attempts = []

    async def drop_first(request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ReadError(
                "Server disconnected without sending a response.", request=request
            )
        return await served.handle_async_request(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(drop_first), base_url="http://m")
    bundle = await read_bundle(client, DEMO_BUNDLE)
    assert bundle.bundle_id == DEMO_BUNDLE and attempts == [f"/bundles/{DEMO_BUNDLE}"] * 2

    attempts.clear()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadError("x"))),
        base_url="http://m",
    )
    with pytest.raises(BundleError, match=r"\(ReadError\)"):
        await read_bundle(client, DEMO_BUNDLE)

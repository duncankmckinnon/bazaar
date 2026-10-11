"""EDGAR's closed days decide whether an acceptance label is evidence, and when a filing is public."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from bazaar_market.sources.edgar import (
    EASTERN,
    check_acceptance_label,
    edgar_closed,
    federal_holidays,
    visible_at,
)
from bazaar_market.sources.errors import SourceError
from bazaar_market.sources.models import Filing


def filing(accepted: str, filed: str, form: str = "8-K") -> Filing:
    return Filing(
        cik=1,
        accession="a-1",
        form=form,
        report_date=None,
        filing_date=date.fromisoformat(filed),
        accepted_at=datetime.fromisoformat(accepted),
        primary_document="d.htm",
    )


def test_the_2026_federal_holidays_match_the_sec_edgar_calendar():
    # https://www.sec.gov/submit-filings/filer-support-resources/edgar-calendar, 2026 list
    assert sorted(federal_holidays(2026)) == [
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12),
        date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("day", "closed"),
    [
        (date(2021, 12, 31), True),  # New Year's Day 2022 fell on a Saturday
        (date(2024, 12, 24), True),  # ad hoc, cited
        (date(2025, 12, 26), True),  # ad hoc, cited
        (date(2025, 1, 9), True),  # ad hoc, cited
        (date(2026, 10, 10), True),  # Saturday
        (date(2025, 12, 23), False),
        (date(2026, 10, 9), False),
    ],
)
def test_edgar_closed_days(day, closed):
    assert edgar_closed(day) is closed


@pytest.mark.parametrize(
    ("accepted", "filed"),
    [
        ("2025-11-11T07:50:50+00:00", "2025-11-12"),  # Veterans Day overnight (MAA)
        ("2025-12-24T06:08:32+00:00", "2025-12-29"),  # ad hoc closure overnight (NWS)
        ("2014-11-11T05:02:55+00:00", "2014-11-12"),  # Veterans Day 2014 (AXP)
    ],
)
def test_an_overnight_label_on_a_closed_day_proves_nothing(accepted, filed):
    check_acceptance_label(filing(accepted, filed))


def test_an_early_label_on_a_business_day_still_raises():
    with pytest.raises(SourceError, match="earlier than the real acceptance"):
        check_acceptance_label(filing("2025-12-23T13:00:00+00:00", "2025-12-26"))


def test_a_holiday_overnight_label_is_public_from_the_next_business_day_at_06_00():
    late = visible_at(filing("2025-11-11T07:50:50+00:00", "2025-11-12"))  # 02:50 on Veterans Day

    assert late == datetime(2025, 11, 12, 11, tzinfo=UTC)  # 06:00 EST


def test_a_business_day_label_before_06_00_waits_until_06_00_that_day():
    early = visible_at(filing("2026-10-06T06:00:00+00:00", "2026-10-06"))  # 02:00 EDT, Tuesday

    assert early == datetime(2026, 10, 6, 10, tzinfo=UTC)


def test_an_after_hours_label_on_a_business_day_is_kept():
    # 18:05 EDT on a Monday: EDGAR gives it the next filing date but disseminates it at once.
    label = "2026-10-05T22:05:00+00:00"

    assert visible_at(filing(label, "2026-10-06")) == datetime.fromisoformat(label)


@pytest.mark.parametrize(
    ("label", "public"),
    [
        # Saturday 01:00 EDT -> Monday 06:00 EDT.
        ("2026-10-03T05:00:00+00:00", datetime(2026, 10, 5, 10, tzinfo=UTC)),
        # Saturday before Presidents' Day -> Tuesday 06:00 EST (the ANET 10-K).
        ("2026-02-14T05:12:46+00:00", datetime(2026, 2, 17, 11, tzinfo=UTC)),
    ],
)
def test_a_weekend_label_waits_for_the_next_business_day(label, public):
    assert visible_at(filing(label, "2026-02-17")) == public


def test_visibility_is_never_earlier_than_the_label_and_always_inside_dissemination():
    at = datetime(2024, 12, 20, tzinfo=UTC)
    while at < datetime(2026, 12, 31, tzinfo=UTC):
        public = visible_at(filing(at.isoformat(), "2026-01-01"))
        local = public.astimezone(EASTERN)
        assert public >= at
        assert not edgar_closed(local.date()) and local.time() >= time(6)
        assert visible_at(filing(public.isoformat(), "2026-01-01")) == public
        at += timedelta(minutes=97)  # walks every time of day across two years

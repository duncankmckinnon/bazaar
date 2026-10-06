from datetime import UTC, date, datetime

import pytest
from bazaar_market.sources.models import Fact, Filing, NewsItem
from bazaar_market.sources.visibility import (
    fact_available_at,
    visible_facts,
    visible_filings,
    visible_news,
)


def filing(accession, form, accepted_at):
    return Filing(
        cik=719739,
        accession=accession,
        form=form,
        report_date=date(2022, 12, 31),
        filing_date=accepted_at.date(),
        accepted_at=accepted_at,
        primary_document="doc.htm",
    )


def fact(accession, value, filed):
    return Fact(
        cik=719739,
        taxonomy="us-gaap",
        concept="NetIncomeLoss",
        unit="USD",
        value=value,
        period_start=date(2022, 1, 1),
        period_end=date(2022, 12, 31),
        fiscal_year=2022,
        fiscal_period="FY",
        form="10-K",
        accession=accession,
        filed=filed,
    )


ORIGINAL = filing("0000719739-23-000021", "10-K", datetime(2023, 2, 24, 21, 43, 8, tzinfo=UTC))
AMENDED = filing("0000719739-23-000030", "10-K/A", datetime(2023, 3, 1, 14, 0, 0, tzinfo=UTC))


def test_a_filing_is_hidden_one_second_before_it_was_accepted():
    as_of = datetime(2023, 2, 24, 21, 43, 7, tzinfo=UTC)

    assert visible_filings([ORIGINAL], as_of) == []


def test_a_filing_is_visible_at_the_second_it_was_accepted():
    as_of = datetime(2023, 2, 24, 21, 43, 8, tzinfo=UTC)

    assert visible_filings([ORIGINAL], as_of) == [ORIGINAL]


def test_an_amendment_stays_hidden_until_its_own_acceptance():
    between = datetime(2023, 2, 27, 12, 0, 0, tzinfo=UTC)
    after = datetime(2023, 3, 1, 14, 0, 0, tzinfo=UTC)

    assert visible_filings([AMENDED, ORIGINAL], between) == [ORIGINAL]
    assert visible_filings([AMENDED, ORIGINAL], after) == [ORIGINAL, AMENDED]


def test_a_fact_becomes_available_when_its_filing_was_accepted():
    f = fact(ORIGINAL.accession, 1_672_000_000, date(2023, 2, 24))

    assert fact_available_at(f, [ORIGINAL]) == datetime(2023, 2, 24, 21, 43, 8, tzinfo=UTC)


def test_a_fact_from_an_unknown_filing_waits_until_the_day_after_it_was_filed():
    f = fact("0000000000-00-000000", 1.0, date(2023, 2, 24))

    assert fact_available_at(f, [ORIGINAL]) == datetime(2023, 2, 25, 0, 0, 0, tzinfo=UTC)


def test_a_restated_value_is_hidden_until_the_amendment_is_accepted():
    first = fact(ORIGINAL.accession, 1_672_000_000, date(2023, 2, 24))
    restated = fact(AMENDED.accession, 1_509_000_000, date(2023, 3, 1))
    between = datetime(2023, 2, 27, 12, 0, 0, tzinfo=UTC)

    assert visible_facts([first, restated], [ORIGINAL, AMENDED], between) == [first]


def test_a_revised_article_is_hidden_until_its_last_revision():
    item = NewsItem(
        source="alpaca",
        id="37447136",
        symbols=("AAPL",),
        headline="h",
        body="b",
        created_at=datetime(2024, 3, 4, 1, 31, 31, tzinfo=UTC),
        updated_at=datetime(2024, 3, 4, 1, 32, 12, tzinfo=UTC),
    )

    assert visible_news([item], datetime(2024, 3, 4, 1, 32, 0, tzinfo=UTC)) == []
    assert visible_news([item], datetime(2024, 3, 4, 1, 32, 12, tzinfo=UTC)) == [item]


def test_a_simulated_time_without_a_timezone_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        visible_filings([ORIGINAL], datetime(2023, 3, 1, 0, 0, 0))  # noqa: DTZ001

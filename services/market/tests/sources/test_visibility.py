from datetime import UTC, date, datetime

import pytest
from bazaar_market.sources.models import Fact, Filing, NewsItem
from bazaar_market.sources.visibility import (
    fact_available_at,
    visible_facts,
    visible_filings,
    visible_news,
)
from pydantic import ValidationError


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


def test_facts_and_news_also_reject_a_simulated_time_without_a_timezone():
    naive = datetime(2023, 3, 1, 0, 0, 0)  # noqa: DTZ001

    with pytest.raises(ValueError, match="timezone"):
        visible_facts([], [ORIGINAL], naive)
    with pytest.raises(ValueError, match="timezone"):
        visible_news([], naive)


def news(id_, updated_at):
    return NewsItem(
        source="alpaca",
        id=id_,
        symbols=("AAPL",),
        headline="h",
        body="b",
        created_at=datetime(2024, 3, 1, tzinfo=UTC),
        updated_at=updated_at,
    )


def test_visible_news_is_ordered_by_the_time_each_article_became_readable():
    late = news("late", datetime(2024, 3, 4, 12, 0, tzinfo=UTC))
    early = news("early", datetime(2024, 3, 4, 9, 0, tzinfo=UTC))

    result = visible_news([late, early], datetime(2024, 3, 5, tzinfo=UTC))

    assert [n.id for n in result] == ["early", "late"]


def test_a_record_cannot_be_built_with_a_timestamp_that_has_no_timezone():
    with pytest.raises(ValidationError):
        filing("x", "10-K", datetime(2023, 2, 24, 21, 43, 8))  # noqa: DTZ001


def test_visible_facts_handles_many_facts_against_many_filings_quickly():
    filings = [filing(f"acc-{i}", "8-K", datetime(2023, 1, 1, tzinfo=UTC)) for i in range(4000)]
    facts = [fact(f"acc-{i}", float(i), date(2023, 1, 1)) for i in range(4000)]

    visible = visible_facts(facts, filings, datetime(2023, 6, 1, tzinfo=UTC))

    assert len(visible) == 4000

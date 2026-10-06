import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from bazaar_market.prices import (
    Bar,
    BarConflict,
    FutureDataError,
    MissingData,
    SqliteMarketData,
    close_at,
    import_bars,
    main,
    read_bars_csv,
    synthetic_bars,
    write_bars_csv,
)

MON, TUE, WED = date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)


def bar(day, close, symbol="AAPL"):
    price = Decimal(close)
    return Bar(symbol, day, price, price, price, price, 1000)


BARS = [bar(MON, "100.00"), bar(TUE, "101.50"), bar(WED, "99.25")]


def load(tmp_path, bars=BARS, version="test-v1"):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        import_bars(connection, bars, data_version=version, source="fixture")
    return SqliteMarketData(path, version)


def count(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM data_bars").fetchone()[0]


def test_a_daily_close_is_available_at_1600_eastern_in_both_seasons():
    assert close_at(date(2025, 7, 1)) == datetime(2025, 7, 1, 20, 0, tzinfo=UTC)
    assert close_at(MON) == datetime(2026, 2, 2, 21, 0, tzinfo=UTC)


def test_price_at_includes_a_close_at_the_exact_instant_it_became_available(tmp_path):
    observation = load(tmp_path).price_at("AAPL", close_at(TUE))

    assert (observation.observed_at, observation.available_at) == (close_at(TUE), close_at(TUE))
    assert observation.price == Decimal("101.50")


def test_price_at_one_microsecond_before_a_close_returns_the_previous_close(tmp_path):
    observation = load(tmp_path).price_at("AAPL", close_at(TUE) - timedelta(microseconds=1))

    assert observation.price == Decimal("100.00")


def test_price_at_before_the_first_close_raises(tmp_path):
    with pytest.raises(MissingData):
        load(tmp_path).price_at("AAPL", close_at(MON) - timedelta(microseconds=1))


def test_price_at_for_a_symbol_with_no_bars_raises(tmp_path):
    with pytest.raises(MissingData, match="MSFT"):
        load(tmp_path).price_at("MSFT", close_at(WED))


def test_price_at_reads_only_its_own_data_version(tmp_path):
    load(tmp_path, [bar(MON, "5.00")], version="other-v1")

    assert load(tmp_path).price_at("AAPL", close_at(MON)).price == Decimal("100.00")


def test_reimporting_the_same_bars_adds_nothing_and_survives_reopening(tmp_path):
    market = load(tmp_path)
    with closing(sqlite3.connect(market.database_path)) as connection:
        added = import_bars(connection, BARS, data_version="test-v1", source="fixture")

    assert (added, count(market.database_path)) == (0, 3)
    assert SqliteMarketData(market.database_path, "test-v1").price_at("AAPL", close_at(WED))


def test_importing_a_changed_bar_under_the_same_version_raises_and_writes_nothing(tmp_path):
    market = load(tmp_path)
    with (
        closing(sqlite3.connect(market.database_path)) as connection,
        pytest.raises(BarConflict),
    ):
        import_bars(
            connection,
            [bar(date(2026, 2, 5), "1.00"), bar(TUE, "999.00")],
            data_version="test-v1",
            source="fixture",
        )

    assert count(market.database_path) == 3
    assert market.price_at("AAPL", close_at(TUE)).price == Decimal("101.50")


def test_price_history_includes_both_ends_of_the_window(tmp_path):
    history = load(tmp_path).price_history(
        "AAPL", close_at(MON), close_at(TUE), cutoff=close_at(WED), limit=100
    )

    assert [o.price for o in history] == [Decimal("100.00"), Decimal("101.50")]


def test_price_history_past_the_cutoff_raises(tmp_path):
    with pytest.raises(FutureDataError):
        load(tmp_path).price_history(
            "AAPL",
            close_at(MON),
            close_at(TUE) + timedelta(microseconds=1),
            cutoff=close_at(TUE),
            limit=100,
        )


def test_price_history_stops_at_the_limit(tmp_path):
    history = load(tmp_path).price_history(
        "AAPL", close_at(MON), close_at(WED), cutoff=close_at(WED), limit=2
    )

    assert [o.observed_at for o in history] == [close_at(MON), close_at(TUE)]


def test_a_day_without_bars_has_no_session(tmp_path):
    market = load(tmp_path)

    assert market.session(MON).close_at == close_at(MON)
    assert market.session(date(2026, 2, 7)) is None


def test_price_source_is_the_source_the_bars_were_imported_with(tmp_path):
    assert load(tmp_path).price_source == "fixture"


def test_the_bar_csv_round_trips(tmp_path):
    write_bars_csv(tmp_path / "bars.csv", BARS)

    assert read_bars_csv(tmp_path / "bars.csv") == BARS


def test_a_bar_csv_with_other_columns_is_refused(tmp_path):
    (tmp_path / "bars.csv").write_text("symbol,day,close\nAAPL,2026-02-02,1\n")

    with pytest.raises(ValueError, match="columns"):
        read_bars_csv(tmp_path / "bars.csv")


def test_synthetic_bars_are_the_same_on_every_run():
    assert synthetic_bars(["AAPL"]) == synthetic_bars(["AAPL"])


def test_synthetic_bars_cover_the_runner_demo_window_on_every_weekday():
    bars = synthetic_bars()
    window = [date(2026, 1, 30) + timedelta(days=d) for d in range(15)]
    weekdays = {d for d in window if d.weekday() < 5}

    for symbol in ("AAPL", "MSFT", "KO"):
        assert {b.session for b in bars if b.symbol == symbol} >= weekdays


def test_synthetic_bars_follow_the_demo_ticker_history():
    bars = synthetic_bars(["FISV", "K"])
    fiserv = {b.symbol for b in bars if b.session < date(2025, 11, 11)}

    assert fiserv == {"FI", "K"}
    assert {b.symbol for b in bars if b.session >= date(2025, 11, 11)} == {"FISV", "K"}
    assert max(b.session for b in bars if b.symbol == "K") == date(2025, 12, 10)


def test_the_command_line_writes_and_imports_the_synthetic_set(tmp_path):
    csv_path, db_path = tmp_path / "bars.csv", tmp_path / "db" / "market.sqlite3"

    main(["synthetic", "--out", str(csv_path)])
    main(["import", str(csv_path), "--db", str(db_path)])
    main(["import", str(csv_path), "--db", str(db_path)])

    market = SqliteMarketData(db_path, "synthetic-v1")
    assert count(db_path) == len(synthetic_bars())
    assert market.price_at("KO", close_at(date(2026, 2, 13))).observed_at == close_at(
        date(2026, 2, 13)
    )


def test_synthetic_prices_answer_at_every_close_of_the_runner_demo_window(tmp_path):
    path = tmp_path / "market.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        import_bars(connection, synthetic_bars(), data_version="synthetic-v1", source="synthetic")
    market = SqliteMarketData(path, "synthetic-v1")
    window = [date(2026, 1, 30) + timedelta(days=d) for d in range(15)]

    for symbol in ("AAPL", "MSFT", "KO"):
        for day in (d for d in window if d.weekday() < 5):
            assert market.price_at(symbol, close_at(day)).observed_at == close_at(day)

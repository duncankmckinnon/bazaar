from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bazaar_market.prices import MissingData
from bazaar_protocol import PriceObservation

DAY1_CLOSE = datetime(2025, 7, 1, 20, 0, tzinfo=UTC)
DAY2_CLOSE = DAY1_CLOSE + timedelta(days=1)


def close(at: datetime, price: str) -> PriceObservation:
    return PriceObservation(observed_at=at, available_at=at, price=Decimal(price))


class FakePrices:
    """Synthetic daily closes. `price_at` returns the latest close available by the cutoff."""

    data_version = "fixture-v1"
    price_source = "fixture"

    def __init__(self, bars: dict[str, list[PriceObservation]], on_price=None) -> None:
        self.bars = bars
        self.on_price = on_price

    def price_at(self, symbol: str, cutoff: datetime) -> PriceObservation:
        if self.on_price is not None:
            self.on_price()
        visible = [bar for bar in self.bars.get(symbol, []) if bar.available_at <= cutoff]
        if not visible:
            raise MissingData(symbol)
        return max(visible, key=lambda bar: bar.available_at)


BARS = {
    "AAPL": [close(DAY1_CLOSE, "100.00"), close(DAY2_CLOSE, "110.00")],
    "TIE": [close(DAY1_CLOSE, "100.005")],
}


def catalog(prices: FakePrices):
    """A `prices_for` lookup that knows only `prices.data_version`."""

    def prices_for(data_version: str) -> FakePrices:
        if data_version != prices.data_version:
            raise MissingData(data_version)
        return prices

    return prices_for

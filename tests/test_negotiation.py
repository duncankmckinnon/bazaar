from bazaar.agents import Buyer, Seller
from bazaar.ledger import Ledger
from bazaar.market import SimulatedFeed
from bazaar.metrics import deal_count
from bazaar.models import Commodity, Motive
from bazaar.negotiation import negotiate


def test_negotiation_closes_and_records():
    copper = Commodity(symbol="CU")
    feed = SimulatedFeed({"CU": 9500.0}, volatility=0, seed=1)
    seller = Seller("seller", Motive(quantity_target=50))
    buyer = Buyer("buyer", Motive(quantity_target=50))
    outcome = negotiate(copper, seller, buyer, feed)
    assert outcome.deal is not None
    ledger = Ledger()
    ledger.record(outcome.deal)
    assert deal_count(ledger) == 1

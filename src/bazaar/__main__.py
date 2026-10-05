from __future__ import annotations

from .agents import Buyer, Seller
from .ledger import Ledger
from .market import SimulatedFeed
from .metrics import avg_premium_to_market, deal_count
from .models import Commodity, Motive
from .negotiation import negotiate


def main() -> None:
    copper = Commodity(symbol="CU")
    feed = SimulatedFeed({"CU": 9500.0}, seed=0)
    ledger = Ledger()
    for _ in range(20):
        seller = Seller("seller", Motive(quantity_target=100, urgency=0.7))
        buyer = Buyer("buyer", Motive(quantity_target=100, urgency=0.4))
        outcome = negotiate(copper, seller, buyer, feed)
        if outcome.deal:
            ledger.record(outcome.deal)
    print(f"deals: {deal_count(ledger)}/20, avg premium to market: {avg_premium_to_market(ledger)}")


if __name__ == "__main__":
    main()

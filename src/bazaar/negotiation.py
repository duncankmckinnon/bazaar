from __future__ import annotations

from .agents import Agent
from .market import MarketFeed
from .models import Commodity, Deal, Outcome


def negotiate(commodity: Commodity, seller: Agent, buyer: Agent, feed: MarketFeed) -> Outcome:
    quote = feed.quote(commodity)
    s_offer = seller.opening(quote)
    b_offer = buyer.opening(quote)
    transcript = [s_offer, b_offer]
    max_rounds = min(seller.strategy.max_rounds, buyer.strategy.max_rounds)

    for round_no in range(1, max_rounds + 1):
        for actor, last, own_last in ((seller, b_offer, s_offer), (buyer, s_offer, b_offer)):
            reply = actor.respond(quote, last, own_last)
            if reply == "walk":
                return Outcome(transcript=transcript)
            if reply == "accept":
                deal = Deal(
                    commodity=commodity,
                    quantity=min(s_offer.quantity, b_offer.quantity),
                    price=last.price,
                    market_price=quote.price,
                    rounds=round_no,
                )
                return Outcome(deal=deal, transcript=transcript)
            transcript.append(reply)
            if actor is seller:
                s_offer = reply
            else:
                b_offer = reply
    return Outcome(transcript=transcript)

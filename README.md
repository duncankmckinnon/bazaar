# Bazaar

"Sell me this pen", but for commodities. A **seller agent** tries to sell inventory to a **buyer agent**.
Both see current market prices, but each has private motives: the seller has sales quotas, the buyer
has inventory needs. They negotiate over multiple rounds, and every deal (or walk-away) is recorded in
a ledger that the agents use to improve their own strategies, criteria, tools and metrics.

## Layout

| Module | Purpose |
| --- | --- |
| `bazaar.models` | Core types: `Commodity`, `Quote`, `Offer`, `Deal`, `Motive` |
| `bazaar.market` | `MarketFeed` protocol for up-to-date prices, plus a simulated feed |
| `bazaar.agents` | `Seller` and `Buyer` agent definitions |
| `bazaar.negotiation` | The round-by-round negotiation loop |
| `bazaar.ledger` | Historical transaction store (SQLite) |
| `bazaar.strategy` | Strategy and criteria that agents revise from ledger history |
| `bazaar.metrics` | Metrics and dashboard data derived from the ledger |

## Quick start

```sh
uv sync
uv run pytest
uv run bazaar
```

## Roadmap

- [ ] LLM-backed seller and buyer (Pydantic AI)
- [ ] Real market price feed adapters
- [ ] Strategy reflection step: agents review ledger and rewrite their own criteria
- [ ] Agent-built tools and dashboards
- [ ] Evals across strategies and market regimes

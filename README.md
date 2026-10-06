# Bazaar

Bazaar's target is historical stock strategy experimentation with fake-money accounts in a market
DB. Agents buy and sell through simulated market execution using point-in-time prices, news,
prior-cycle company reports and Pydantic Monty. An orchestrator proposes the next strategy/test plan
for human approval, supported by Logfire + AI Gateway traces, evaluations, datasets and oversight.
A planned FastAPI management interface registers strategies as new named agents; registration does
not start an experiment.

The current scaffold runs a market container and two placeholder agents named `seller` and `buyer`.

**Status: scaffolding.** The containers build and run and the agents can reach the market. Market
data, agent behavior and messaging are not implemented yet. The target design is in
`docs/superpowers/specs/`; `docs/superpowers/plans/` contains an archived skeleton plan.

The [current provisional specification](docs/superpowers/specs/bazaar-evaluation-and-optimization-design.md)
describes historical accounts/execution, full-timeline trade evaluation, point-in-time research,
harness comparisons and approval before testing. It supersedes the earlier peer-sale marketplace
and skeleton assumptions; the old implementation plan needs revision before use for this direction.

| Container | Source | Purpose |
| --- | --- | --- |
| `market` | `services/market` | FastAPI service (`/health` for now); will own prices, accounts and history |
| `seller`, `buyer` | `services/agent` | One image run twice; will be Pydantic AI agents with their own directives |

## Run

```sh
docker compose up --build
curl localhost:8000/health
```

## Shared API contracts

`packages/protocol` provides the Pydantic models used by both services. The initial agreement is
immediate fill or rejection, with market-owned accounts and point-in-time data. See the
[market–agent API agreement](docs/market-agent-api.md) for routes, examples and safety rules.
The trading endpoints are specified, not implemented yet.

## Data sources

`bazaar_market.sources` freezes SEC EDGAR filings, Alpaca news and S&P 500 membership to `data/raw/` and provides
the point-in-time visibility rules for reading them. See [data sources](docs/data-sources.md).

## Develop

```sh
uv sync --all-packages
uv run pytest
uv run ruff check
```

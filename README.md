# Bazaar

Two trading agents (a seller and a buyer) negotiate commodity trades through a simulated market.
Each runs in its own Docker container.

**Status: scaffolding.** The containers build and run and the agents can reach the market. Market
data, agent behavior and messaging are not implemented yet. The target design is in
`docs/superpowers/specs/` and a fuller implementation plan is in `docs/superpowers/plans/`.

| Container | Source | Purpose |
| --- | --- | --- |
| `market` | `services/market` | FastAPI service (`/health` for now); will own prices, accounts and history |
| `seller`, `buyer` | `services/agent` | One image run twice; will be Pydantic AI agents with their own directives |

## Run

```sh
docker compose up --build
curl localhost:8000/health
```

## Develop

```sh
uv sync --all-packages
uv run pytest
uv run ruff check
```

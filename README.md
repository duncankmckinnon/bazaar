# Bazaar

Bazaar's target is historical stock strategy experimentation with fake-money accounts in a market
DB. Agents buy and sell through simulated market execution using point-in-time prices, news,
prior-cycle company reports and optional sandboxed Code Mode. An orchestrator proposes the next
strategy/test plan for human approval, supported by Logfire + AI Gateway traces, evaluations,
datasets and oversight.
The agent-side FastAPI interface registers strategies as new named agents; registration does not
start an experiment.

The current scaffold runs an agent-side registry API, a market container and two placeholder agents
named `seller` and `buyer`.

**Status: registry + fixture trading harness.** Strategy registration, retrieval and immutable
versioning, scoped research clients and a bounded PydanticAI decision loop are implemented.
The [trading harness guide](docs/trading-agent.md) describes local fixture usage, tool capabilities,
budgets and order recovery. Registration and the placeholder processes still do not execute
strategies. Optional Code Mode replaces the custom calculation surface: it can call scoped
sequential research reads and compute from their returned data, while `market_order` remains a
native tool. Sandbox research stubs are synchronous functions called without `await`.
Enable it with trusted `RuntimeConfig(code_mode=True)`; it is off by default and shares the
existing decision budgets.
Live market handlers, historical experiments, gateway binding and approvals
are not implemented yet. The target design is in
`docs/superpowers/specs/`; `docs/superpowers/plans/` contains an archived skeleton plan.

The [current provisional specification](docs/superpowers/specs/bazaar-evaluation-and-optimization-design.md)
describes historical accounts/execution, full-timeline trade evaluation, point-in-time research,
harness comparisons and approval before testing. It supersedes the earlier peer-sale marketplace
and skeleton assumptions; the old implementation plan needs revision before use for this direction.

| Container | Source | Purpose |
| --- | --- | --- |
| `market` | `services/market` | FastAPI service (`/health` for now); will own prices, accounts and history |
| `seller`, `buyer` | `services/agent` | One image run twice; will be Pydantic AI agents with their own directives |
| `api` | `services/agent` | Named-agent and strategy registry, with its own persistent SQLite volume; independent of the market |

## Run

```sh
docker compose up --build
curl localhost:8000/health
curl localhost:8001/health
```

## Strategy registration

The registry runs separately from trading agents via `python -m bazaar_agent.api`, listening on
port 8001. Compose binds it to localhost and persists its own `registry-data` volume. It never calls
or changes the market, provisions accounts, loads submitted code, or executes a strategy.

For local development:

```sh
uv run --package bazaar-agent python -m bazaar_agent.api
```

See the [registration API guide](docs/strategy-registration-api.md) for payloads, idempotency,
versioning and configuration. **This local API has no authentication; do not expose it publicly.**

Pydantic Logfire monitors API requests, registry operations, database queries and agent HTTP/logging
activity. Set `LOGFIRE_TOKEN` to enable export; token-free local operation and tests remain supported.
Sensitive strategy payloads are excluded from traces. See the API guide's monitoring section.

## Shared API contracts

`packages/protocol` provides the Pydantic models used by both services. The initial agreement is
immediate fill or rejection, with market-owned accounts and point-in-time data. See the
[market–agent API agreement](docs/market-agent-api.md) for routes, examples and safety rules.
The trading endpoints are specified, not implemented yet.

## Develop

```sh
uv sync --all-packages
uv run pytest
uv run ruff check
```

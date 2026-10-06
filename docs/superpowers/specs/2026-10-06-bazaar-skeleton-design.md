# Bazaar skeleton design

## Goal

Replace the single-package prototype with a Docker project of three independent containers: a **market** service and two **agent** instances (a seller and a buyer) that negotiate in free-form language through the market. This spec covers the skeleton only: structure, wire models, and a stack that boots, with the minimum behavior to prove the pieces connect. Everything else is listed under Deferred.

## Decisions

- **Market is the exchange.** Agents only know the market URL. It serves prices, holds accounts and relays messages. Settlement of trades is deferred but the data model includes it.
- **Symmetric agents.** One agent image, two instances with different directives files. No hard-coded seller or buyer role in code; each agent's goals, starting account and persona come from its directives.
- **Participants, not roles.** The market's unit is a participant (`id`, `kind` = `agent` | `human`). Messages are addressed participant to participant. A terminal or voice client for a third party is a future participant and needs no market or agent changes. No such client is built now.
- **Free-form messages.** A message is `{id, thread_id, from_id, to_id, text, sent_at}`. The market never parses text. Structured trades are a later commitment step.
- **Autonomous loop.** Each agent runs continuously: poll the market for new messages, run the Pydantic AI agent when there is something to react to, sleep otherwise.
- **State ownership.** The market owns accounts and holdings. An agent's private notes and message cursor live in a local SQLite file on its own volume (notes are deferred; the cursor is in the skeleton).
- **Untrusted input.** Counterparty messages are passed to the model as quoted data. Hard limits come from directives and market checks, not model judgment.

## Layout

```
bazaar/
├── docker-compose.yml        # market + agent-seller + agent-buyer, volumes, .env
├── .env.example              # LOGFIRE_TOKEN, LOGFIRE_READ_TOKEN, model API key
├── libs/protocol/            # pydantic wire models only (installed into each image)
├── services/market/          # FastAPI + SQLite, Dockerfile, market.yaml
├── services/agent/           # Pydantic AI loop, market client, Dockerfile
├── directives/               # seller.yaml, buyer.yaml
└── tests/                    # per-service tests + compose smoke test
```

The existing `src/bazaar`, `tests/test_negotiation.py` and root `pyproject.toml` are removed. CI is updated to lint, test and build the images.

## Components in the skeleton

### libs/protocol
Pydantic models: `Participant`, `Account`, `Holding`, `PricePoint`, `Message`, `Trade` (model only). No logic.

### services/market
- SQLite schema for all tables: `participants`, `accounts`, `holdings`, `prices`, `messages`, `trades`.
- `market.yaml` seeds commodities with starting prices and participants with opening cash and holdings. Seeding is idempotent, so a restart does not reset state.
- Background price generator: seeded log-normal random walk per commodity, stepped every N seconds, appended to `prices`.
- Endpoints: `GET /health`, `GET /prices`, `GET /prices/{symbol}/history`, `GET /participants`, `GET /accounts/{id}`, `POST /messages`, `GET /messages?for=<id>&since=<cursor>`.
- Messages are readable only by sender and recipient (checked via the `for` parameter; real auth is deferred).

### services/agent
- Loads a directives YAML: participant id, starting account (sent to the market only through its seeding config), goals and persona text.
- Market client (httpx) with typed methods for the skeleton endpoints; waits for `/health` on startup.
- A Pydantic AI `Agent` whose system prompt is built from the directives, with three tools: `get_prices`, `get_my_account`, `send_message`.
- Loop: poll messages since the persisted cursor, run the agent if any are new or a self-wake timer fires, advance the cursor only after a successful run, sleep. Tool errors from the market return as readable tool results; model failures leave the cursor unchanged.
- Logfire: `logfire.configure()`, `instrument_pydantic_ai()` and httpx instrumentation, with `participant_id` as a resource attribute. Runs without a token (no export) for local work.

## Testing

- Market: TestClient on an in-memory DB covers seeding idempotency, price stepping with a fixed seed, message privacy and cursor behavior.
- Agent: Pydantic AI `TestModel`/`FunctionModel` against a fake market client covers tool wiring and cursor-on-failure.
- Smoke: `docker compose up` with a scripted model, asserting both agents register as healthy and a message sent by one is received by the other. No real LLM calls.

## Deferred (not in the skeleton)

Trade propose/accept and settlement, `list_trades`, per-counterparty notes tools, `query_logfire`, `wait` tool and self-directed proactive behavior beyond the timer stub, proposal expiry, per-run tool and token caps, market regimes, auth, terminal and voice clients, evals, and real price feeds.

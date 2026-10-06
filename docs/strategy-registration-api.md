# Agent-side strategy registration API

This API lives in `services/agent` (`bazaar_agent.api`) and has its own SQLite registry. It does not
call or modify the market. Trading-agent execution remains a different entry point.

**Local development only: no authentication or approval mechanism is implemented here.** Compose
binds port 8001 to loopback. `created_by: "local-api"` is server-generated provenance, not an
authenticated identity. Registration and revision never fund accounts, launch experiments, execute
model calls, or import an artifact's code.

## Configuration

- `BAZAAR_REGISTRY_DB_PATH`: registry SQLite file; default `data/registry.sqlite3`, `/data/registry.sqlite3`
  in Compose. Compose uses the separate `registry-data` volume.
- `BAZAAR_REGISTRY_MODEL_REFS`: comma-separated configured model references; default `test`. These
  are catalog references for later execution, not credentials or claims that a gateway route is
  currently available. Unknown references are rejected before writes.
- `BAZAAR_API_HOST`: default `127.0.0.1`; Compose sets `0.0.0.0` inside the container while binding
  the host port only to `127.0.0.1`. The API port is 8001.

## Logfire monitoring

The API and agent entry points configure Pydantic Logfire once per process. Set `LOGFIRE_TOKEN`
through your environment to export to its project; Compose passes it to the API and trading-agent
containers. No project is hard-coded and no token is stored in this repository. Without a token
(or existing local Logfire credentials), local execution and tests do not send telemetry.

Services are named `bazaar-agent-api`, `bazaar-agent-seller`, and `bazaar-agent-buyer`; version and
`BAZAAR_ENVIRONMENT` (default `development`) identify deployments. Monitoring includes FastAPI
request duration/status, registry operation spans, SQLite query spans, registration/version/replay
metadata, validation/conflict/storage events, system metrics, and agent HTTPX/logging telemetry.
Health/docs requests are excluded from API traces.

Instructions, hypotheses, artifact references, request bodies, SQL parameters and HTTP headers are
not captured. Operation argument extraction is disabled. Agent/strategy/version IDs and configured
model/harness references identify behavior without serializing the definition. Telemetry tests use
Logfire's in-memory exporter to verify request/operation/SQL tracing and sensitive-payload exclusion;
no real model or Logfire credentials are needed. No AI decision spans are fabricated for the current
polling placeholders; Pydantic AI model/decision instrumentation comes with the real trading harness.

## Create a named strategy

```sh
curl -sS localhost:8001/strategies \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 00000000-0000-0000-0000-000000000001' \
  -d '{
    "name": "Alpha Trader",
    "description": "Initial historical strategy",
    "definition": {
      "harness": "single_shot",
      "model_ref": "test",
      "instructions": "Maximize portfolio value using only permitted historical evidence.",
      "tools": ["account", "market_history", "orders"]
    }
  }'
```

HTTP 201 returns `status: "proposed"`, `agent`, `strategy`, and initial `version` records. Agent,
strategy, and version have separate UUIDs. The version number starts at 1. Agent names are stripped,
lowercased, with whitespace/underscores replaced by hyphens; they must start with a letter and
contain only ASCII lowercase letters, digits and hyphens, up to 64 characters. Names are unique.

Definitions support harnesses `single_shot`, `orchestrated`, `monty`, and `research`. Tool references
are `account`, `market_history`, `orders`, `news`, `reports`, `monty`, and `private_history`. Defaults
include account/history/order tools. Duplicate/unknown tools are rejected; tool order is normalized.
An optional `artifact_ref` stores an opaque reviewed-artifact reference; it is never downloaded or
executed. No arbitrary executable-code, credential, actor or free-form settings fields are accepted.

`parent_version_id` optionally links a new strategy to an existing version. Parent references must
exist. Creating a child does not change or share its parent's experiment account.

## Revision and retrieval

| Endpoint | Behavior |
| --- | --- |
| `GET /agents` / `GET /agents/{agent_id}` | Named agent identities and their strategy references |
| `GET /strategies` / `GET /strategies/{strategy_id}` | Strategy metadata, latest version ID and version count |
| `POST /strategies/{strategy_id}/versions` | New immutable version; required `definition`, optional `hypothesis` and `parent_version_id` |
| `GET /strategies/{strategy_id}/versions` | Paginated immutable versions, ascending version number |
| `GET /strategies/{strategy_id}/versions/{version_id}` | One exact version, scoped to its strategy |
| `GET /health` / `GET /docs` | Health and generated OpenAPI UI |

POST revisions require a fresh `Idempotency-Key` UUID. If no parent is supplied, the new version
points to the latest version at transaction time. Explicit cross-strategy parent references are
allowed for provenance. A revision preserves the agent identity, old definition/version data and
old retrieval results; it does not update a running strategy.

Lists return `{items, total, limit, offset}`; default limit 20, maximum 100, nonnegative offset.
Agents/strategies sort by creation time then ID. Description/hypothesis/instructions have bounded
lengths. Unknown request fields are rejected. UUID/time fields serialize normally through the
shared `bazaar_protocol.registry` models.

Each version stores a SHA-256 `definition_digest` of canonical normalized definition JSON, creation
time and server provenance. This hashes the definition/reference, **not external artifact bytes**;
actual artifact review/approval is future work.

## Idempotency, persistence and errors

Identical normalized requests with the same key return the original HTTP 201 response, including
original IDs/timestamps, even after later versions exist. Changed content conflicts. Initial
creation keys are registry-wide; revision keys are scoped to the strategy. Never generate a new
key automatically after an ambiguous network failure.

Agent + strategy + version + replay result creation is one transaction. Failed writes leave no
orphan agent or consumed key. SQLite serializes concurrent writes so version numbers and default
parent lineage stay consistent. All records/replay results survive restarts; no update/delete
endpoints expose old versions.

Failures use `ApiError` (`error.code`, `message`, `retryable`): invalid fields/header/model references
422, duplicate normalized names or changed-content keys 409, missing parent/agent/strategy/version
404, and storage failures 500. Invalid bodies and internal storage details are not echoed.

Component tests cover atomic rollback, retries, persistence, parallel writes, version immutability,
lineage, pagination, validation and the absence of account/experiment execution side effects. Full
approval/experiment behavior belongs to later agent-side work, not this registry API.

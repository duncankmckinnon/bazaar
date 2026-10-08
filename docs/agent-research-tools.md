# Agent research tools (#21)

## Implemented client surface, not market handlers

`bazaar_agent.research` exports `ResearchTools`, `ResearchContext`, `FiscalCycle`,
`ToolResult[T]`, `ToolError`, `PrivateHistoryReader`, `UnavailablePrivateHistory`, and
`PrivateHistoryUnavailable`. #24 can register the bound async methods as tools without a
PydanticAI harness dependency here. Arguments are shared Pydantic DTOs, not raw JSON or SQL:

- `account()`, `portfolio()` return the existing snapshot DTOs.
- `place_order(OrderRequest)` returns the existing discriminated immediate fill/reject result.
- `prices(PriceHistoryRequest)` uses the existing price route/query agreement.
- `news(ResearchRequest)`, `filings(ResearchRequest)` return typed archive pages.
- `account_history(HistoryRequest)`, `portfolio_history(HistoryRequest)`, `orders(HistoryRequest)`
  return typed historical pages (orders have terminal results, not pending orders).
- `private_history(HistoryRequest)` uses a scoped typed adapter; its default is **unsupported**.

Pass an `httpx.AsyncClient` with a fixed market base URL, bounded timeout and out-of-band
credentials. The caller owns its lifecycle. Requests never follow redirects or automatically
retry orders. Preserve `client_order_id` on an explicit retry after an ambiguous failure.
Transport/API errors discard server bodies and exception text; rejected order messages are
replaced with their enum codes. Successful archived content is intentionally returned to the
trading model. Tool decorators do not extract arguments, but nested SDK and instrumented HTTPX
spans are not suppressed. PydanticAI decision traces can include research content, and HTTP spans
can include cursor query strings or exception details before the tool catches an error. Configure
a trusted Logfire destination and do not attach hooks/transports that independently log credentials.
Tool spans use `extract_args=False`; ordinary transport/hook exceptions are caught inside the
span with fixed messages. Cancellation still propagates. Local preflight errors are
`invalid_request`, distinct from invalid server responses.
No real model, external search provider, database credentials, approval or auth implementation is added.

## Trusted context and defensive validation

The runner supplies `ExperimentContext` and per-company `FiscalCycle(symbol, start)` boundaries.
A filing must end strictly before that company's current cycle starts, and its publication,
revision and actual availability must all be no later than the simulated cutoff. A missing or
ambiguous company calendar is unsupported; calendar/fiscal cycles are not guessed from dates.
These are dependencies, **not tool arguments**. Recreate tools on clock, event sequence, scope,
strategy or source-version changes. Do not carry cached model state or cursor state across runs.
The client takes a validated copy of context and does not maintain account/portfolio state.
An old idempotent order result is settlement evidence, not a replacement for a current balance.

All returned data must match scope, cutoff, version, query window and page limit. Pages must be
ordered with unique record IDs; continuations must be ordered beyond the previous page and may
not repeat IDs. Opaque cursors can only be used after issuance for that exact route/window/symbol/
limit and context. Repeated reads may reissue a stable cursor only with the same query, source,
accumulated IDs and boundary. A payload digest additionally requires identical page replays,
including terminal pages; no page content is cached. Callers must treat repeated reads as replays,
not append duplicate records to their history. Cursor cycles/incompatible reuse are rejected. No automatic page
walking or unbounded retrieval. Archive queries select publication timestamps (private history
selects simulated timestamps), with inclusive UTC start/end. A daily news request uses a UTC day
window capped at the cutoff; revision/availability filtering still applies to its text/headline.
A revision or delayed release beyond cutoff rejects the **whole response**, never merely removes
its timestamp while returning its snippet. Even empty archives require explicit complete coverage.
Incomplete coverage returns `missing_data`, distinct from a complete archive with zero records.

Private notes/caches/traces/attempts require the same page scope and provenance checks, plus
simulated timestamps and runner event sequence at or before the supplied context. Earlier
wall-clock execution is not eligibility. The typed adapter has no free-form query, project key,
full-timeline or evaluator endpoint. #23 must bind a protected SDK reader that enforces tenant,
experiment, account, strategy and simulated-time scope remotely, including inherited cache
validation/reset. Synthetic fake-adapter tests here do not prove real private-store authorization.

**Client validation is not authorization.** It cannot detect a dishonest source that labels future
text with past timestamps, nor establish completeness from price bars alone. The server/importer
must establish trusted publication/revision evidence, historical fiscal calendars, complete archive
coverage, immutable source versions and authorization. Errors/unknown fields fail closed; text is
untrusted research, not tool instructions. No future comparison labels are part of these DTOs.

## Proposed backend integration (not implemented)

Main's market service still exposes only `/health`. Existing account/portfolio/order/price routes
are the agreement in [market-agent-api.md](market-agent-api.md), not working endpoints. The
following routes are **new proposals**, implemented only as client calls against MockTransport:

| Method / route under `/experiments/{experiment_id}` | Proposed response |
| --- | --- |
| `GET /news/{symbol}` | `NewsPage` |
| `GET /filings/{symbol}` | `FilingPage` |
| `GET /accounts/{account_id}/history` | `AccountHistoryPage` |
| `GET /accounts/{account_id}/portfolio/history` | `PortfolioHistoryPage` |
| `GET /accounts/{account_id}/orders` | `OrderHistoryPage` |

Queries are `start_at`, `end_at`, `limit`, optional `cursor`; symbols only come from validated paths.
`bazaar_protocol.research` defines lean frozen/extra-forbidden DTOs, including `HistoryPage[T]`
with scope, cutoff, requested coverage window, source/version and `coverage` status. Complete
coverage is for the entire query window, not just the records on one page. Provenance records
carry stable IDs and revision IDs; news/filing content includes publication, revision and
availability timestamps. Provenance-bearing archive items must match the page source. Account/
portfolio/order history envelope source identifies the history store, while embedded mark/fill
sources may identify a different price provider; embedded source versions must still match the
trusted context. Snapshot identity is simulated timestamp plus state version, not state version
alone. Servers must choose only eligible versions and never backfill a modern
snippet into an old version. Missing supported routes return safe `missing_data` (404) or
`unsupported` (501), not an invented success. The existing price DTO does not have a coverage
flag: explicit missing-price coverage must be a server error; empty price history is not proof
of complete coverage. Adding price coverage would require a separate contract revision.

PR #37 was inspected read-only. Its source `NewsItem.created_at/updated_at` and filing
`accepted_at`/accession align conceptually with publication/revision/release filtering; its frozen
snapshot manifests can supply source/version evidence. They are **not** these wire DTOs or HTTP
handlers. An importer must map ticker changes, SEC accession/revision relationships and company
fiscal cycles, supply actual availability evidence and distinguish partial captures. #37 has no
prior-cycle eligibility rule and no account/execution endpoints. No changes to that PR, market
service, market Compose service or issue dependencies are required by this client-only change.

Backend integration must prove cross-account/future rejection, cursor binding and complete
coverage, valid live grants, historical execution and idempotent atomic settlement. #18 remains
deferred; #21 adds neither approval nor authentication. Unit fixtures cannot establish any of
those server guarantees. Do not run historical experiments using these mock contracts alone.

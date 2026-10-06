# Market–agent API agreement (v0.1)

These contracts live in `bazaar-protocol`, a uv workspace package consumed by both services.
This change defines the agreement, not a database, execution engine, approval service or new HTTP
handlers. The existing `/health` endpoint is unchanged.

## Initial execution mode

Orders are **immediately filled in full or rejected**. There are no pending orders, partial fills,
reservations or cancellation endpoints in this version. This is the selected initial scope for
issue 15; add those features through a later explicit contract revision if needed.

The market owns account state and atomically validates **actual execution cost**, including fees,
before updating cash and inventory. Concurrent orders cannot spend/sell the same resources. A buy
requires `quantity * unit_price + fee <= cash`; a sell requires sufficient owned quantity and must
not leave negative cash after fees. A rejection changes neither balances nor account state version.
The client does not set a price, balance, approval, strategy version or simulated clock.

Market execution rules determine eligible historical prices at the trusted simulated time. Never
observe a future bar/close and retroactively fill at an earlier favorable price. Price-source,
data-version and execution-rule references accompany every fill. Validation of these rules and
accounting is a **server responsibility**; wire validation alone is not authorization or settlement.

## Common wire rules

- Identity references are UUIDs: agent, strategy version, experiment, account, approval and order.
- Decimal money/share quantities serialize as JSON **strings**. Inputs accept decimal strings and
  integral numbers (or Python `Decimal` values), but reject binary floating-point numbers and
  booleans. Money and portfolio marks are finite and nonnegative; order/holding quantities and
  execution prices are finite and positive. Fractional shares are representable; allowed
  precision/rounding belongs to the manifest. `portfolio_value` is calculated by the server under
  its `valuation_rule_version`, not recomputed with ambient decimal rounding by wire validation.
- Timestamps are timezone-aware **UTC** ISO 8601 strings. Simulated and wall-clock timestamps are
  distinct; these market snapshots use simulated time.
- Unknown fields are rejected. Snapshots are frozen and use tuple collections (JSON arrays).
- `state_version` is a nonnegative integer incremented by the market for successful state changes.
  Responses include authoritative snapshots; clients must not replace newer state with an older
  replayed snapshot.
- Symbols use uppercase letters/digits/dot/hyphen, starting with a letter, at most 16 characters.
  Source symbol mapping belongs to the importer, not the trading model.

## Endpoint agreement

Routes below are for subsequent implementations; they are not available yet. Protected endpoints
require an authenticated identity scoped to the experiment/account. The server derives its
`ExperimentContext` from the authenticated scope, manifest, live approval and runner-controlled
clock. That model is **not** an agent request body or an approval claim to trust.

| Method and route | Request / response |
| --- | --- |
| `GET /experiments/{experiment_id}/accounts/{account_id}` | `AccountSnapshot` at the server-controlled clock |
| `POST /experiments/{experiment_id}/accounts/{account_id}/orders` | `OrderRequest` → discriminated `OrderResult` |
| `GET /experiments/{experiment_id}/accounts/{account_id}/portfolio` | `PortfolioSnapshot` with cutoff-safe marks and source/version evidence |
| `GET /experiments/{experiment_id}/prices/{symbol}` | `PriceHistoryRequest` fields `start_at`, `end_at`, `limit`, optional `cursor` as query parameters; symbol from path → `PriceHistory` |

`PriceHistoryRequest` represents the combined validated path/query input. Page size defaults to
100 and is bounded at 1000; integer query strings such as `limit=100` are accepted. `start_at <= end_at`; asking for an end beyond the trusted cutoff is
rejected, not permission to advance time. Responses are ordered by observation time, with unique
observation timestamps and availability no later than the response cutoff. A page can include a
`next_cursor`; continuation requests pass it as `cursor`, and the market must bind that opaque token
to the original query, source version, cutoff and authorized experiment. Clients cannot edit it to
retrieve future data. News/filing-specific contracts will extend this package with the same
availability boundary; they are not invented in this first order/account agreement.

## Example: buy 10 AAPL

Request to the account's order endpoint:

```json
{
  "client_order_id": "00000000-0000-0000-0000-000000000005",
  "symbol": "AAPL",
  "side": "buy",
  "quantity": "10"
}
```

Successful result (opening cash was `$10,000`; zero simulated fees):

```json
{
  "status": "filled",
  "order_id": "00000000-0000-0000-0000-000000000006",
  "client_order_id": "00000000-0000-0000-0000-000000000005",
  "symbol": "AAPL",
  "side": "buy",
  "quantity": "10",
  "unit_price": "74.20",
  "fee": "0",
  "executed_at": "2020-01-02T14:30:00Z",
  "price_observed_at": "2020-01-02T14:30:00Z",
  "price_available_at": "2020-01-02T14:30:00Z",
  "price_source": "fixture",
  "data_version": "fixture-v1",
  "execution_rule_version": "immediate-v1",
  "account": {
    "account_id": "00000000-0000-0000-0000-000000000001",
    "agent_id": "00000000-0000-0000-0000-000000000002",
    "experiment_id": "00000000-0000-0000-0000-000000000003",
    "strategy_version_id": "00000000-0000-0000-0000-000000000004",
    "simulated_at": "2020-01-02T14:30:00Z",
    "state_version": 1,
    "currency": "USD",
    "cash": "9258.00",
    "holdings": [{"symbol": "AAPL", "quantity": "10"}]
  }
}
```

A rejected order has `status: "rejected"`, order/request IDs, requested symbol/side/quantity,
`rejected_at`, a structured `error` and the unchanged `account` snapshot. Both domain outcomes
return HTTP 200. Clients parse either with `order_result_adapter.validate_json(...)`.

## Errors, replay and safety

Transport/authentication/validation failures use `ApiError`, for example:

```json
{"error": {"code": "idempotency_conflict", "message": "Different order body", "retryable": false}}
```

Use HTTP 401 for missing authentication; 403 for unauthorized scope, future-data access or a
missing/expired/revoked approval; 404 for unavailable authorized resources; 409 for idempotency
conflicts or an experiment not running; 422 for malformed inputs. Execution rejections such as
insufficient cash/holdings, market closure or unavailable price data use `RejectedOrder`.
Errors must not expose another account, secret or evaluator-only outcome.

`client_order_id` is an idempotency key scoped to the authenticated account and experiment.
The market stores the original validated request and terminal result atomically. A same-content
retry returns the original order/result without another debit/fill; different content conflicts.
Replays still require valid authorized access; an old result is evidence of an earlier settlement,
not permission for a new one. Do not automatically retry with a fresh ID after an ambiguous network
failure. Lookup/replay must not silently replace the original result with today's balance.

Market account provisioning, clock advancement and any evaluator full-timeline reads are separately
privileged operations, not agent tools in this contract. Validate live grants at protected writes;
a cached context cannot bypass expiry/revocation or scope exhaustion. The orchestrator may propose
strategies but cannot approve or execute them. Atomic resource checks and historical-information
boundaries need market integration tests when the backend lands.

## Verification

Protocol tests are in the existing market test suite. They check JSON round trips, discriminated
result schemas, malformed/unknown inputs, finite amounts, UTC/identity/window constraints,
availability cutoffs and account/portfolio consistency. No real model, market feed or Logfire
credentials are needed. Existing health/connectivity tests remain unchanged.

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

## Market-wide reads (v5)

Agreed between the market and the runner on 2026-10-11, and amended the same day: `as_of_session`
comes from the bars calendar, not the window's sessions, so day 1's open reads the previous close. The market implements these routes in
`services/market/src/bazaar_market/market_api.py`. They live under
`/experiments/{experiment_id}/market/`, and, like prices, every one needs `X-Bazaar-Approval` for
that experiment (401 without it, 403 `experiment_not_approved` for another experiment's). The
approval is checked before the parameters.

**Point in time.** There is no client `as_of`. The server computes everything at the
experiment's cutoff:

- `as_of_session` is the latest session whose 16:00 America/New_York close is at or before the
  cutoff, on the calendar of the bundle's bars version: a day on which some bar was observed at
  its close. It is found the same way as the session the fill engine fills at
  (`ledger.latest_session`), so a quote and a fill at the same cutoff use the same close. A cutoff
  1 µs before a close gives the previous session; a cutoff at the close gives that session.
- `as_of_session` can be **before the window start**. At the open of the first window day
  (2026-09-28 13:30Z for demo-bundle-v2), it is the last close before it, 2026-09-25. Past the
  window end, it stays at the last session with bars.
- `available_at` does not choose the session. It decides which values are shown: a bar's close,
  volume and changes are used only when its `available_at <= cutoff`. A bar not yet available at
  the cutoff shows as missing (`no_bar_at_cutoff`, or a null change), even when its session has
  closed, and its day still counts as a session.
- When no session with bars has closed within 14 days before the cutoff, screen, quotes and
  sectors answer 409 `experiment_not_running`, which is the case in which the fill engine
  rejects with `market_closed`. Search is static and still answers.
- The sessions before `as_of_session` are the days on which any member has a bar, which is the
  same rule the ledger uses for an open market. Every one of them closed before the cutoff.
  `pct_change_1d` is close(`as_of_session`) / close(previous session) − 1, and `pct_change_5d`
  uses the close five sessions earlier. A change is `null` when either bar is missing, never 0
  and never measured against an earlier session instead.

**Wire rules.** Closes are decimal strings as stored. Percent values are strings with 2 decimal
places, rounded half-even once (`0.02125` gives `"2.12"`). An unknown or repeated query parameter
gives 422. An experiment whose data version has no stored universe (a plain bars version) gives
404 `data_unavailable`, and an unknown experiment gives 404 `not_found`.

Screen, quotes and sectors share this envelope:

```json
{"cutoff_at": "2026-10-02T20:00:00Z", "as_of_session": "2026-10-02", "data_version": "demo-bundle-v2"}
```

| Route | Query | Response (envelope plus) |
| --- | --- | --- |
| `GET .../market/screen` | `sort=pct_change_1d\|pct_change_5d\|volume` (default `pct_change_1d`), `direction=desc\|asc` (default `desc`), `sector=<GICS name>`, `limit=1..50` (default 20) | `sort`, `direction`, `sector` (string or null), `excluded`, `rows: [{symbol, name, sector, close, pct_change_1d, pct_change_5d, volume, observed_at}]` |
| `GET .../market/quotes` | `symbols=A,B` (1 to 50) | `quotes: [{symbol, name, sector, close, observed_at, available_at, pct_change_1d, pct_change_5d, volume}]`, `missing: [{symbol, reason}]` |
| `GET .../market/search` | `q` (1 to 64 characters), `limit=1..25` (default 10) | no envelope: `{data_version, query, results: [{symbol, name, sector}]}` |
| `GET .../market/sectors` | none | `sectors: [{sector, symbols, with_bars, avg_pct_change_1d, avg_pct_change_5d, advancers, decliners, unchanged}]`, `benchmark: {symbol, close, pct_change_1d, pct_change_5d, observed_at}` |

**Screen.** It covers the universe only and never lists the benchmark. A symbol whose sort key is
null (no bar at `as_of_session`, or not enough history) is left out and counted in `excluded`.
Other fields in a row may be null. Rows are ordered by the unrounded sort key, then by symbol
ascending in both directions. `sector` must exactly match a GICS sector name in the universe, or
the request gets 422.

**Quotes.** Symbols are uppercased and deduplicated, and keep request order. More than 50
distinct symbols, an empty item or a malformed symbol gives 422. An unknown symbol is not an
error. It is listed in `missing` with reason `unknown_symbol`, and a member without a bar at
`as_of_session` gets reason `no_bar_at_cutoff`. The benchmark (SPY) can be quoted, with sector
`"Benchmark"`.

**Search.** It is case-insensitive over the universe and the benchmark, and uses static metadata
only, with no prices. Results are ranked by exact symbol, then symbol prefix, then name substring,
then sector substring, and then by symbol.

**Sectors.** Each sector is equal-weighted over its constituents, ordered by sector name.
`symbols` counts constituents and `with_bars` counts those with a bar at `as_of_session`. The
averages are means of the unrounded changes, rounded once, over the constituents whose change is
not null. Advancers, decliners and unchanged count the sign of `pct_change_1d` and skip nulls.
`benchmark` has null fields when SPY has no bar at `as_of_session`. It is null for a bundle with
no benchmark (demo-bundle-v1).

**Bundles.** `GET /bundles/{bundle_id}` (no credentials) returns `bundle_id`, `bars`, `news`,
`filings`, `start_date`, `end_date`, `universe_name` (`"S&P 500"`, or null for v1),
`universe: [{symbol, name, sector}]` (the benchmark is not in it), `benchmark` (`"SPY"`, or null
for v1), `expected_sessions`, `sessions` and `incomplete_sessions: [{date, missing}]`. Sessions
are judged over the universe and the benchmark.

**Universe.** The demo-bundle-v2 universe is the S&P 500 constituents as of 2026-09-25 (Wikipedia
revision of that date or earlier) that have a bar on every session the bundle needs: every window
session, plus the lookback its first day needs for 5-day changes. A member removed from the index
during the window is therefore excluded for the whole window. WBD was removed effective
2026-10-06 and its last bar is 2026-10-05, so agents never see it, not even from 09-28 to 10-05.
This is a deliberate, small survivorship choice. No future price is ever shown, but membership is
decided with knowledge of the window's end. It is **not** point-in-time index membership, and
evaluators must not treat it as such. The exclusions are listed in `data_universe_exclusions` and
in the coverage report's ticker changes.

News and filings are unchanged and per symbol (`/news/{symbol}`, `/filings/{symbol}`). A
multi-symbol news tool calls them once per symbol. SPY is traded through the normal order path.

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

## Agent research client

The agent-side typed client and proposed archive/history extensions are described separately in
[agent-research-tools.md](agent-research-tools.md). Those proposals add no market handlers and do
not change the existing route agreement or its server-side authorization responsibilities.

## Verification

Protocol tests are in the existing market test suite. They check JSON round trips, discriminated
result schemas, malformed/unknown inputs, finite amounts, UTC/identity/window constraints,
availability cutoffs and account/portfolio consistency. No real model, market feed or Logfire
credentials are needed. Existing health/connectivity tests remain unchanged.

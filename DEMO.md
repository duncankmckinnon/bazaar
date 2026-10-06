# Bazaar demo branch (`demo/aie-nyc`)

This branch integrates the open PRs into one working system for the AI Engineer NYC demo. It is
not meant to merge as a whole: each piece is reviewed in its own PR. Use it to run the full loop
locally and to plug the trading agent in.

Status as of 2026-10-06 evening (US Eastern).

## What runs today

Each agent gets a funded account and trades autonomously, deciding for itself, until the run ends
or it runs out of money. Its objective is to maximize its portfolio balance. One command runs four
launches over the same simulated period (AAPL, MSFT and KO, 2026-02-02 to 2026-02-13, $10,000
each), every one in its own experiment with its own approval:

| Launch | Policy | How it trades |
| --- | --- | --- |
| agent | `agent-fixture-v1` | Duncan's `run_decision` with a scripted `FunctionModel` (no LLM yet). At the first decision it reads 3 AAPL news items, then places its own order (buy 10 AAPL) under a runner-reserved `client_order_id`; afterwards it holds. |
| agent | `scripted-momentum-v1` | a scripted policy; the runner submits its orders. Drop it with `--no-momentum`. |
| baseline | `baseline-buy-and-hold` | the runner submits an equal-weight basket at the first decision |
| baseline | `baseline-cash-only` | never trades |

The agent's HTTP client carries only `X-Bazaar-Approval` and `X-Bazaar-Account`, never the runner
token. If a decision errors, the runner looks the reserved id up in the account's order history, so
an order is recorded once or not at all, and the run continues. The runner refuses to start if two
launches share an experiment or approval id. All launches use one `--data-version` (default
`demo-bundle-v1`: prices, news and filings), so the leaderboard can compare them.

Each launch writes `runs/<run>/record.json` (what happened) and `evaluation.json` (scores), and the
leaderboard ranks them. The runner also has a `--refused-demo` flag that launches with an
unapproved id to test the approval check. It is not part of the demo.

## How the pieces fit

Each simulated morning the runner moves the market clock to 09:30, asks the agent for a decision,
and the agent's orders go to the market, which fills or rejects each one in a single transaction.
At each 16:00 close the runner reads the portfolio. After the run the evaluator scores it and the
leaderboard ranks it.

| Piece | Where | PR | Owns |
| --- | --- | --- | --- |
| Market | `services/market` | #41 (on #37) | prices, the per-experiment clock, accounts, orders, auth |
| Runner | `services/runner` | #42 | simulated time, the run loop, run records, the demo CLI |
| Evaluator | `packages/evaluation` | #43 | per-trade and period scores, exact reconciliation |
| Baselines and leaderboard | `packages/replay` | #44 | cash-only, buy-and-hold, `leaderboard.html` |
| Agent harness and research tools | `services/agent` | #38, #39 | `run_decision`, scoped market and research clients |
| Registry | `services/agent` | #36 (merged) | named strategies and versions; not wired into runs yet |

The boundaries are the point of the demo. The agent never moves the clock (only the runner holds
`X-Bazaar-Runner-Token`), never sees prices after the cutoff (the market refuses them with 403),
never changes a balance except by placing an order, and never sees its scores.

## Branch-only differences from the PRs

- **Dev approval allow-list** (commit `919ac28`). `BAZAAR_DEV_APPROVAL_IDS` lists
  `<approval_id>:<experiment_id>` pairs the market accepts until the approval service (#18) exists.
  It is reverted in #41. **Never carry it into a PR.**
- **`scripts/market-dry-run.py`**: a live HTTP check of the runner's call path. It needs the
  allow-list, so it lives only here.
- **Merged here before review:** #38 and #39 (Duncan's research tools and decision harness), with
  #39 at `7edaecd`. #39 has newer commits than that, which are not merged here yet. #40 (Monty) is not
  merged, because it conflicts with #39 in `services/agent/src/bazaar_agent/trading.py`.

## Quickstart (synthetic prices, no keys)

Run from the repository root. Tested on this branch at `5d345bc`.

```sh
uv sync --all-packages

# 1. Prices: synthetic bars for the 13 demo companies.
uv run python -m bazaar_market.prices synthetic --out data/bars-synthetic-v1.csv
uv run python -m bazaar_market.prices import data/bars-synthetic-v1.csv --db data/market.sqlite3

# 2. Credentials: a runner token, and one approved experiment per launch.
export BAZAAR_RUNNER_TOKEN="$(openssl rand -hex 32)"
for run in AGENT_FIXTURE MOMENTUM BUY_AND_HOLD CASH_ONLY; do
  export "BAZAAR_${run}_EXPERIMENT_ID=$(uuidgen)" "BAZAAR_${run}_APPROVAL_ID=$(uuidgen)"
done
export BAZAAR_DEV_APPROVAL_IDS="$BAZAAR_AGENT_FIXTURE_APPROVAL_ID:$BAZAAR_AGENT_FIXTURE_EXPERIMENT_ID,$BAZAAR_MOMENTUM_APPROVAL_ID:$BAZAAR_MOMENTUM_EXPERIMENT_ID,$BAZAAR_BUY_AND_HOLD_APPROVAL_ID:$BAZAAR_BUY_AND_HOLD_EXPERIMENT_ID,$BAZAAR_CASH_ONLY_APPROVAL_ID:$BAZAAR_CASH_ONLY_EXPERIMENT_ID"

# 3. Market, in the background.
BAZAAR_MARKET_DB=data/market.sqlite3 uv run uvicorn bazaar_market.app:app --port 8000 > market.log 2>&1 &
until curl -sf localhost:8000/health >/dev/null; do sleep 0.5; done

# 4. Runs, then the leaderboard.
uv run python -m bazaar_runner --demo --data-version synthetic-v1 --runs-dir runs
uv run python -m bazaar_replay.leaderboard runs -o leaderboard.html
```

The runner prints one line per launch, for example
`baseline-cash-only <experiment_id>: completed, final value 10000.00`.
It reads the eight `BAZAAR_*_ID` variables; each also has a flag (`--agent-fixture-experiment-id`,
`--momentum-experiment-id` and so on). The old `BAZAAR_AGENT_*` and `--agent-*` names still set the
momentum launch, with a deprecation warning. Every approval must be on `BAZAAR_DEV_APPROVAL_IDS`, or
the market refuses that launch at its first clock call.

Synthetic prices have no news, so on `synthetic-v1` the fixture agent's news read fails
(`missing_data`), the decision is recorded under `decision_errors`, and the agent does not trade.
Use real data (below) to see it place its order.

## Real prices

Fetching Alpaca daily bars needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Follow
`services/market/README.md` section 1, then use `--data-version alpaca-bars-v1` in step 4. Bars are
unadjusted on purpose (adjusted history bakes in later corporate actions). Downloaded data stays
under `data/` and is gitignored; the provider terms have not been reviewed, so never commit it.

`demo-bundle-v1` maps three imports in one market DB: bars `alpaca-bars-v1`, news `alpaca-news-v1`
and filings `edgar-filings-v1` (the import commands' defaults). To reproduce it, after the bars in
`services/market/README.md` section 1:

```sh
# News (reads ALPACA_API_KEY and ALPACA_SECRET_KEY)
uv run --env-file .env python -m bazaar_market.sources news --version news-2026-10-06
uv run python -m bazaar_market.sources import-news \
    --snapshot data/raw/alpaca-news/news-2026-10-06 --db data/market.sqlite3

# Filings (EDGAR needs SEC_USER_AGENT with a contact address; no API key)
uv run --env-file .env python -m bazaar_market.sources edgar --version edgar-2026-10-06
uv run python -m bazaar_market.sources import-filings \
    --snapshot data/raw/edgar/edgar-2026-10-06 --db data/market.sqlite3   # needs market fb5519c or later
```

Which imports the bundle needs:

- **Bars are required.** Every launch prices through them.
- **News is needed for the agent to trade.** Without it the fixture agent's news read is
  `missing_data`, the decision is recorded under `decision_errors`, and it places no order. The
  baselines and momentum are unaffected.
- **Filings are optional.** Without them `GET /fiscal-cycles` returns `[]`, and the agent's
  `filings()` is unsupported. Nothing else changes.

Then run step 4 with `--data-version demo-bundle-v1` (the default). On 2026-10-06 at `f697d06` that gave: cash-only 0.00%, agent-fixture -0.37% (1 fill: 10 AAPL
at 259.48, placed by the agent), buy-and-hold -0.98%, scripted momentum -1.36% (15 fills). Every run
reconciled to the cent. The baselines and momentum match the earlier bars-only run exactly, also
after the market's fill-rule change in `3d7af80`.

## Logfire

Authenticate once from the repository root, then start the market and the runner from the same
directory, so they find the project credentials. Nothing is sent without credentials.

```sh
uv run logfire auth
uv run logfire projects use --org logfire bazaar-demo
```

Setting `LOGFIRE_TOKEN` (a project write token) in both processes' environment also works.
Each run is one trace (run, decision, order and mark spans), and the evaluator adds one span per
trade. The trace id is stored in `record.json` and shown on the leaderboard.

Note for the agent: `run_decision` currently disables instrumentation inside a decision, so model
and tool calls do not appear as spans.

## Tests

```sh
uv run pytest        # 964 passed at b923ed6
uv run ruff check
```

## In progress tonight

| Work | Branch | Who |
| --- | --- | --- |
| Live run with filings imported, so the agent's `filings()` sees real cycles | `demo/aie-nyc` | runner team |

Done tonight: the agent places its own order (runner `53c0366`, `7ab56c3`, `0a1d121`); order history,
account and portfolio history, and news routes (market, merged); the leaderboard flags runs with
decision errors (replay `380ff22`); the runner reads `GET /fiscal-cycles` at each decision and passes
the cycles to `run_decision` (runner `4e6dc18`; `--no-fiscal-cycles` turns it off); filings route,
fiscal cycles and the filings coverage fix (market `3c386a8`, `ded3a01`, `fb5519c`, `6c3c706`). A
cycle start counts only filings accepted by the experiment's cutoff, using PR #37's acceptance-time
check.

Research runs will use `data_version=demo-bundle-v1` (prices, news and filings together). For now
the agent's client sends `X-Bazaar-Account` on news and filings, because those routes have no
account in the path.

## Open questions for Duncan

1. Can `run_decision` be the agent slot as wired here (approval-only client, reserved order id,
   reconciliation by order history)?
2. Data version: one bundle id per experiment, with the archive version in each item's `source`?
3. Account on news and filings: keep the header, or add the account to the route?
4. Fiscal cycles: start = the day after the latest visible 10-K/10-Q period end, so in effect a
   filing is visible from its acceptance. "Prior fiscal year only" would be a stricter rule that you
   would define. Which do you want?
5. `trading.py`: which side wins between #39 and #40?
6. Logfire: can `run_decision` opt in to model and tool spans with payloads redacted (#23)?
7. A real model: today `run_decision` accepts only `TestModel` and `FunctionModel`. What should
   the Gateway model factory look like?
8. `ResearchTools` pins identical queries, so `orders()` after `market_order()` in one decision
   fails with `invalid_response` (the history legitimately changed). The runner reconciles it, but
   should the pins be cleared after an order?
9. The default `DecisionBudget` (4 model requests, 12 tool calls, 16,000 total tokens, 30 s) is used
   up by one real news page before an order: `news(limit=100)` with 100 articles of about 2k
   characters cost 37,461 tokens by the 2nd model request, so no order was placed. `limit=3` cost
   2,860 tokens and the order went through. That is about 950 tokens per article (a `FunctionModel`
   estimate, not a real tokenizer). Raise the defaults, trim article bodies, or page smaller?
10. Market approvals are bound to the experiment, not the account, so they are safe only with one
    agent per experiment (the runner enforces that). #18 should issue account-scoped credentials.

## Rules for agents working on this branch

- Do not push to this branch directly. Coordinate through Anthony.
- Never move the dev allow-list, `scripts/market-dry-run.py` or downloaded data into a PR.
- Do not read `.env` or print `BAZAAR_RUNNER_TOKEN`, `LOGFIRE_TOKEN` or Alpaca keys.
- Tests stay offline: no real LLM calls, no network, no credentials.

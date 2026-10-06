# Bazaar demo branch (`demo/aie-nyc`)

This branch integrates the open PRs into one working system for the AI Engineer NYC demo. It is
not meant to merge as a whole: each piece is reviewed in its own PR. Use it to run the full loop
locally and to plug the trading agent in.

Status as of 2026-10-06 evening (US Eastern).

## What runs today

Each agent gets a funded account and trades autonomously, deciding for itself, until the run ends
or it runs out of money. Its objective is to maximize its portfolio balance. One command runs three
launches over the same simulated period (AAPL, MSFT and KO, 2026-02-02 to 2026-02-13, $10,000
each):

| Launch | Policy |
| --- | --- |
| agent | `scripted-momentum-v1` (no LLM; the slot the LLM agent plugs into) |
| baseline | `baseline-buy-and-hold` |
| baseline | `baseline-cash-only` |

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

Run from the repository root. Tested on this branch at `3caff3c`.

```sh
uv sync --all-packages

# 1. Prices: synthetic bars for the 13 demo companies.
uv run python -m bazaar_market.prices synthetic --out data/bars-synthetic-v1.csv
uv run python -m bazaar_market.prices import data/bars-synthetic-v1.csv --db data/market.sqlite3

# 2. Credentials: a runner token, and one approved experiment per launch.
export BAZAAR_RUNNER_TOKEN="$(openssl rand -hex 32)"
for run in AGENT BUY_AND_HOLD CASH_ONLY; do
  export "BAZAAR_${run}_EXPERIMENT_ID=$(uuidgen)" "BAZAAR_${run}_APPROVAL_ID=$(uuidgen)"
done
export BAZAAR_DEV_APPROVAL_IDS="$BAZAAR_AGENT_APPROVAL_ID:$BAZAAR_AGENT_EXPERIMENT_ID,$BAZAAR_BUY_AND_HOLD_APPROVAL_ID:$BAZAAR_BUY_AND_HOLD_EXPERIMENT_ID,$BAZAAR_CASH_ONLY_APPROVAL_ID:$BAZAAR_CASH_ONLY_EXPERIMENT_ID"

# 3. Market, in the background.
BAZAAR_MARKET_DB=data/market.sqlite3 uv run uvicorn bazaar_market.app:app --port 8000 > market.log 2>&1 &
until curl -sf localhost:8000/health >/dev/null; do sleep 0.5; done

# 4. Runs, then the leaderboard.
uv run python -m bazaar_runner --demo --data-version synthetic-v1 --runs-dir runs
uv run python -m bazaar_replay.leaderboard runs -o leaderboard.html
```

The runner prints one line per launch, for example
`baseline-cash-only <experiment_id>: completed, final value 10000.00`.
It reads the six `BAZAAR_*_ID` variables; each also has a flag (`--agent-experiment-id` and so
on).

## Real prices

Fetching Alpaca daily bars needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Follow
`services/market/README.md` section 1, then use `--data-version alpaca-bars-v1` in step 4. Bars are
unadjusted on purpose (adjusted history bakes in later corporate actions). Downloaded data stays
under `data/` and is gitignored; the provider terms have not been reviewed, so never commit it.

On 2026-10-06 the real-price run gave: cash-only 0.00%, buy-and-hold -0.98%, scripted momentum
-1.36% (15 fills). Every run reconciled to the cent.

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
uv run pytest        # 804 passed at 3caff3c
uv run ruff check
```

## In progress tonight

| Work | Branch | Who |
| --- | --- | --- |
| The agent places its own order with a runner-reserved `client_order_id`; the runner reconciles through order history | `demo/aie-nyc` | runner team |
| Market routes for the research tools: account orders, account history, portfolio history, news, filings | `feat/market-research` | market team |

Research runs will use `data_version=demo-bundle-v1` (prices, news and filings together). For now
the agent's client sends `X-Bazaar-Account` on news and filings, because those routes have no
account in the path.

## Open questions for Duncan

1. Can `run_decision` be the agent slot as wired here (approval-only client, reserved order id,
   reconciliation by order history)?
2. Data version: one bundle id per experiment, with the archive version in each item's `source`?
3. Account on news and filings: keep the header, or add the account to the route?
4. Fiscal cycles: the market derives a cycle start as the day after the period end of the latest
   10-K or 10-Q accepted by the cutoff (quarterly). Did you mean the fiscal year instead?
5. `trading.py`: which side wins between #39 and #40?
6. Logfire: can `run_decision` opt in to model and tool spans with payloads redacted (#23)?
7. A real model: today `run_decision` accepts only `TestModel` and `FunctionModel`. What should
   the Gateway model factory look like?

## Rules for agents working on this branch

- Do not push to this branch directly. Coordinate through Anthony.
- Never move the dev allow-list, `scripts/market-dry-run.py` or downloaded data into a PR.
- Do not read `.env` or print `BAZAAR_RUNNER_TOKEN`, `LOGFIRE_TOKEN` or Alpaca keys.
- Tests stay offline: no real LLM calls, no network, no credentials.

# Bazaar

Bazaar's target is historical stock strategy experimentation with fake-money accounts in a market
DB. Agents buy and sell through simulated market execution using point-in-time prices, news,
prior-cycle company reports and optional sandboxed Code Mode. An orchestrator proposes the next
strategy/test plan for human approval, supported by Logfire + AI Gateway traces, evaluations,
datasets and oversight.
The agent-side FastAPI interface registers strategies as new named agents; registration does not
start an experiment.

Docker Compose runs the market, strategy registry, and submission web app. The web app's queue
executes the trading runner, PydanticAI agent, outcome scoring, and online strategy-adherence judge.
SQLite databases, frozen source snapshots, and run artifacts live in Docker-managed volumes.

**Status: historical demo trading and strategy evaluation.** Strategy registration, retrieval and immutable
versioning, scoped research clients and a bounded PydanticAI decision loop are implemented.
The [trading harness guide](docs/trading-agent.md) describes local fixture usage, tool capabilities,
budgets and order recovery. Registration alone does not execute strategies; submit them through
the web app to queue a demo run. Optional Code Mode replaces the custom calculation surface: it can call scoped
sequential research reads and compute from their returned data, while `market_order` remains a
native tool. Sandbox research stubs are synchronous functions called without `await`.
Enable it with trusted `RuntimeConfig(code_mode=True)`; it is off by default and shares the
existing decision budgets.
The current demo uses historical data and fake-money execution, not live brokerage trading.
The target design is in
`docs/superpowers/specs/`; `docs/superpowers/plans/` contains an archived skeleton plan.

The [current provisional specification](docs/superpowers/specs/bazaar-evaluation-and-optimization-design.md)
describes historical accounts/execution, full-timeline trade evaluation, point-in-time research,
harness comparisons and approval before testing. It supersedes the earlier peer-sale marketplace
and skeleton assumptions; the old implementation plan needs revision before use for this direction.

| Container | Source | Purpose |
| --- | --- | --- |
| `market` | `services/market` | FastAPI service: daily prices, the experiment clock, accounts and orders, in one SQLite file |
| `web` | `services/web` | Submission UI, leaderboard, queue, trading runner/agent, outcome scoring and online judge |
| `api` | `services/agent` | Named-agent and strategy registry, with its own persistent SQLite volume; independent of the market |
| `data-init` | `services/market` | One-shot snapshot import; the market waits for it to succeed |
| `data-download` | `services/market` | Explicit setup command to download source snapshots into a Docker volume |

## Run

Only Docker with Compose is needed; Python, uv, and SQLite run inside the images.

First-time setup:

1. Copy `.env.example` to `.env` and fill in `BAZAAR_RUNNER_TOKEN` (a long random value),
   `PYDANTIC_AI_GATEWAY_API_KEY`, `LOGFIRE_TOKEN`, the Alpaca credentials, and
   `SEC_USER_AGENT` with your name and contact email. No database URL is required.
2. Download frozen source snapshots into Docker storage:

   ```sh
   docker compose run --rm --build data-download
   ```

   This can take tens of minutes and uses `config/demo-sources.toml`. Bars and news resume
   cached pages when rerun. EDGAR refetches live metadata: if it reports a frozen snapshot
   conflict during initial setup, choose a new `BAZAAR_SNAPSHOT_VERSION` in `.env` and retry.
   Source credentials are used only by this setup container. Once setup succeeds, normal
   application startup uses the stored snapshots without downloading again.
3. Start the entire application:

   ```sh
   docker compose up -d --build
   ```

Open **http://localhost:8080** for the board or **http://localhost:8080/submit** to submit a strategy.
Submissions make billable model calls, including the online judge. The judge exports to Logfire;
there is no local judge-result database. The run record and outcome score are persisted locally.

Subsequent starts and code updates need only `docker compose up -d --build`.
The startup import is repeatable and runs before the market. Missing or invalid snapshots block
startup with an error rather than start an empty trading environment.

```sh
docker compose ps -a
docker compose logs -f data-init market web
docker compose down
```

`down` stops/removes containers but keeps data. **Do not use `down --volumes` unless you intend
to delete all snapshots, databases and runs.** Keep the same Compose project name to reuse them.
All ports bind to localhost: web `8080`, market `8000`, registry `8001`.
The old `seller`/`buyer` health-polling placeholders are no longer started.

LLM providers, Logfire, and first-time source downloads still require external network access;
no external database or database file is needed. Licensed brand fonts are not included in this
checkout; the web UI uses its CSS fallback fonts locally. Run one web container: its queue is
in-process, not a shared multi-replica worker queue.

To run only the market locally, against a SQLite file of your choice:

```sh
BAZAAR_MARKET_DB=data/market.sqlite3 uv run uvicorn bazaar_market.app:app --port 8000
```

Account, order and clock routes need an `X-Bazaar-Approval` header. The runner provisions scoped
experiment grants using the shared runner token; unapproved requests are denied.

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
Registry API traces exclude sensitive strategy payloads. Trading runs include PydanticAI model
inputs/outputs and tool activity; see the [trading guide](docs/trading-agent.md#monitoring-and-next-interfaces)
for telemetry privacy considerations and the API guide's monitoring section for registry tracing.

## Shared API contracts

`packages/protocol` provides the Pydantic models used by both services. The initial agreement is
immediate fill or rejection, with market-owned accounts and point-in-time data. See the
[market–agent API agreement](docs/market-agent-api.md) for routes, examples and safety rules.
The market implements the trading endpoints used by the historical demo runner.

## Data

Source data is not stored in the repo.
With Compose, snapshots live in the `source-snapshots` volume and are imported into `market-data`.
The market serves point-in-time prices, news and filing research over HTTP. The commands below
are the optional host-Python workflow; they are not required for Docker setup.

### Download

1. Install the workspace: `uv sync --all-packages`.
2. Create `.env` from `.env.example` and set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
   Keys from a free Alpaca paper account are enough.
   SEC EDGAR and the membership file need no key.
3. Download the configured sources (bars are a separate command):

   ```sh
   uv run --env-file .env python -m bazaar_market.sources all --version demo
   uv run --env-file .env python -m bazaar_market.sources bars --version demo
   ```

`config/demo-sources.toml` decides what is downloaded: thirteen companies, 2025-07-01 to 2026-09-30.
Edit that file to change the companies or the period.
`--version` names the snapshot. Without it the name is the current UTC minute.

| Command | Downloads | Key needed |
| --- | --- | --- |
| `universe` | S&P 500 membership start and end dates per ticker | no |
| `edgar` | Filing history and reported numbers per company from SEC EDGAR | no |
| `news` | Alpaca news per ticker for the configured period | Alpaca |
| `bars` | Unadjusted daily price bars | Alpaca |
| `all` | The three above | Alpaca |
| `capture-news --days 3` | The trailing days of news, as a new snapshot | Alpaca |

For the demo config the download is about 290 MB and takes roughly 20 minutes. Nearly all of both is news.
If `news` is interrupted, run the same command with the same `--version`.
It continues from the pages already saved.
A version never changes once written: fetching a different period into an existing version is refused.

Filing text is skipped unless the EDGAR User-Agent names a contact address, because the SEC's
document host refuses callers that do not. `config/demo-sources.toml` sets one in `edgar.user_agent`, and
`SEC_USER_AGENT` in `.env` overrides it.
Daily prices are downloaded by `bars`; corporate actions and a dedicated trading calendar
are not downloaded by these commands.

### Where it lands

```
data/raw/
  sp500/a2430f2/sp500_ticker_start_end.csv
  edgar/demo/submissions/CIK0000320193.json        filing history, one or more files per company
  edgar/demo/companyfacts/CIK0000320193.json       reported numbers
  alpaca-news/demo/AAPL/page-0001.json             50 articles per page
  <source>/<version>/manifest.json                 URL, SHA-256, size and fetch time of every file
```

### Read it

```python
from datetime import UTC, date, datetime
from pathlib import Path

from bazaar_market.sources.read import load_facts, load_filings, load_news
from bazaar_market.sources.universe import in_universe, parse_sp500_start_end
from bazaar_market.sources.visibility import visible_facts, visible_filings, visible_news

raw = Path("data/raw")
as_of = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)  # the simulated time

apple = 320193  # SEC company number, listed per ticker in config/demo-sources.toml
filings = load_filings(raw / "edgar/demo", apple, forms=("10-K", "10-Q", "8-K"))
latest = visible_filings(filings, as_of)[-1]
print(latest.form, latest.report_date, latest.accepted_at)

facts = visible_facts(load_facts(raw / "edgar/demo", apple), filings, as_of)
news = visible_news(load_news(raw / "alpaca-news/demo", "AAPL"), as_of)
print(len(facts), "reported values and", len(news), "articles were readable")

members = parse_sp500_start_end((raw / "sp500/a2430f2/sp500_ticker_start_end.csv").read_text())
print(in_universe(members, "AAPL", date(2026, 2, 2)))
```

Run it with `uv run python`.
The `visible_*` functions return only what existed at `as_of`: a filing from its SEC acceptance time
and a news article from its last revision.
A company or ticker that was not downloaded raises `SourceError`. It never reads as an empty list.

[Data sources](docs/data-sources.md) has the rules in full and the known limits.

## Develop

```sh
uv sync --all-packages
uv run pytest
uv run ruff check
```

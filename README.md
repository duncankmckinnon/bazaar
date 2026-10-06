# Bazaar

Bazaar's target is historical stock strategy experimentation with fake-money accounts in a market
DB. Agents buy and sell through simulated market execution using point-in-time prices, news,
prior-cycle company reports and Pydantic Monty. An orchestrator proposes the next strategy/test plan
for human approval, supported by Logfire + AI Gateway traces, evaluations, datasets and oversight.
The agent-side FastAPI interface registers strategies as new named agents; registration does not
start an experiment.

The current scaffold runs an agent-side registry API, a market container and two placeholder agents
named `seller` and `buyer`.

**Status: registry + scaffolding.** Strategy registration, retrieval and immutable versioning are
implemented. Market data, trading behavior, experiments and approvals are not implemented yet. The target design is in
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

## Data

Source data is not stored in the repo.
You download it into `data/raw/`, which git ignores, and read it with `bazaar_market.sources`.
The market service does not serve this data over HTTP yet, so reading it means Python for now.

### Download

1. Install the workspace: `uv sync --all-packages`.
2. Create `.env` from `.env.example` and set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
   Keys from a free Alpaca paper account are enough.
   SEC EDGAR and the membership file need no key.
3. Download everything the config names:

   ```sh
   uv run --env-file .env python -m bazaar_market.sources all --version demo
   ```

`config/demo-sources.toml` decides what is downloaded: thirteen companies, 2025-07-01 to 2026-09-30.
Edit that file to change the companies or the period.
`--version` names the snapshot. Without it the name is the current UTC minute.

| Command | Downloads | Key needed |
| --- | --- | --- |
| `universe` | S&P 500 membership start and end dates per ticker | no |
| `edgar` | Filing history and reported numbers per company from SEC EDGAR | no |
| `news` | Alpaca news per ticker for the configured period | Alpaca |
| `all` | The three above | Alpaca |
| `capture-news --days 3` | The trailing days of news, as a new snapshot | Alpaca |

For the demo config the download is about 290 MB and takes roughly 20 minutes. Nearly all of both is news.
If `news` is interrupted, run the same command with the same `--version`.
It continues from the pages already saved.
A version never changes once written: fetching a different period into an existing version is refused.

Filing text is skipped unless the EDGAR User-Agent names a contact address, because the SEC's
document host refuses callers that do not. `config/demo-sources.toml` sets one in `edgar.user_agent`, and
`SEC_USER_AGENT` in `.env` overrides it.
Prices, corporate actions and the trading calendar are not downloaded yet.

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

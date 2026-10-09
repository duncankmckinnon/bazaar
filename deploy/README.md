# Live deploy on Modal

`deploy/modal_app.py` defines the Modal app `bazaar-live`. One container, with min and max 1, runs the market on
`127.0.0.1:8000` as a subprocess and serves the web app (`bazaar_web.app:app`) on the public Modal URL.
`deploy/live_runtime.py` holds the container logic and has no Modal import, so its tests run offline
(`services/market/tests/test_live_runtime.py`).

Only the PM deploys. Nobody else runs `modal deploy` or `modal serve`.

## What the PM sets

The three local paths, read by `modal_app.py` at deploy time and never committed:

| Variable | Points at | Lands in the container at |
| --- | --- | --- |
| `BAZAAR_DEPLOY_MARKET_DB` | a copy of the market DB with the demo-bundle-v1 data | `/app/seed/market.sqlite3` |
| `BAZAAR_DEPLOY_RUNS` | the folder with the 4 real seed runs | `/app/seed/runs` |
| `BAZAAR_DEPLOY_FONT` | `DwightMedium.woff2` | `/app/fonts/DwightMedium.woff2` (`BAZAAR_FONTS_DIR=/app/fonts`) |

Make the DB copy with `sqlite3 data/market.sqlite3 ".backup /path/to/market-seed.sqlite3"`, which is safe while a
server has the database open.

The Modal secret `bazaar-live`, which Anthony creates. Nobody prints its values. The deploy requires the first three keys:

- `BAZAAR_RUNNER_TOKEN`
- `PYDANTIC_AI_GATEWAY_API_KEY`
- `LOGFIRE_API_KEY`, scoped to `project:read_variables` for `logfire/bazaar-demo`
- `BAZAAR_ADMIN_TOKEN`
- `LOGFIRE_TOKEN`
- `PUBLIC_URL`, the Modal web endpoint URL. Add it after the first deploy prints the URL, then redeploy.

The caps (`BAZAAR_MAX_QUEUE`, `BAZAAR_MAX_SUBMISSIONS_PER_DAY`, `BAZAAR_MAX_PER_IP_PER_HOUR`) can go in the same secret.
Without them the web app uses its defaults.

## The market data bundle

The market DB that `BAZAAR_DEPLOY_MARKET_DB` points at holds `demo-bundle-v1`. It maps three imports
(`services/market/src/bazaar_market/bundles.py`): bars `alpaca-bars-v1`, news `alpaca-news-v1` and filings
`edgar-filings-v1`, which are the import commands' defaults.

- **Bars are required.** Fetch and import them with `services/market/README.md` section 1.
- **News is needed for the agent's news reads.** Without it the market answers `news` with 404 `data_unavailable`.
- **Filings are optional.** Without them `GET /fiscal-cycles` returns `[]` and the agent's `filings()` tool returns
  `unsupported`.

After the bars, import news and filings into the same DB:

```sh
# News (reads ALPACA_API_KEY and ALPACA_SECRET_KEY)
uv run --env-file .env python -m bazaar_market.sources news --version news-2026-10-06
uv run python -m bazaar_market.sources import-news \
    --snapshot data/raw/alpaca-news/news-2026-10-06 --db data/market.sqlite3

# Filings (no API key; EDGAR needs a contact user agent, see below)
uv run --env-file .env python -m bazaar_market.sources edgar --version edgar-2026-10-06
uv run python -m bazaar_market.sources import-filings \
    --snapshot data/raw/edgar/edgar-2026-10-06 --db data/market.sqlite3
```

Bars are unadjusted on purpose: adjusted history rewrites old prices with later corporate actions, which would leak
the future into the past (`sources/alpaca_bars.py`). Raw snapshots under `data/raw/` and the market DB
(`data/*.sqlite3*`) are gitignored. The provider terms have not been reviewed, so never commit downloaded data. The
keys are `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` for Alpaca. EDGAR needs a user agent with a contact address;
`config/demo-sources.toml` sets one, and `SEC_USER_AGENT` overrides it. Never print the keys' values.

## Deploy (PM only)

```sh
BAZAAR_DEPLOY_MARKET_DB=/path/to/market-seed.sqlite3 \
BAZAAR_DEPLOY_RUNS=/path/to/seed-runs \
BAZAAR_DEPLOY_FONT=~/Work/pydantic/brand/fonts/DwightMedium.woff2 \
  uvx modal deploy deploy/modal_app.py
```

Run it from the repository root of the integrated branch, where `services/web` exists. A missing or wrong path stops
the deploy with `set BAZAAR_DEPLOY_... to an existing local path`.

## What the container does

1. The image installs the whole uv workspace from `uv.lock` into the image's system Python
   (`UV_PROJECT_ENVIRONMENT=/usr/local`, `uv sync --frozen --no-dev --all-packages --inexact`), because Modal runs
   functions with that interpreter. `--inexact` keeps the packages Modal's own runtime needs. Local `.venv`, `.env*`,
   `*.sqlite3*` and `data` folders are never added.
2. On start (`@modal.enter`): it refuses to start without `BAZAAR_RUNNER_TOKEN`, without printing any value. It copies
   the seed DB to `/data/market.sqlite3` and the seed runs to `/data/runs` only if they are absent, and logs which
   database is in use: `freshly seeded from ...` or `already on the volume; seed not applied`. Then it starts the
   market and waits up to 30 s for its `/health`. If the market does not come up, the container fails to start.
   If the market dies later, a watchdog logs `the market exited with status N; stopping the container` and exits the
   container non-zero, so Modal starts a new one instead of every run failing against a dead market.
3. The Volume `bazaar-live-data` at `/data` holds `market.sqlite3`, `runs/` and `web.sqlite3`. It is committed every
   10 s, after each scored run, and once at shutdown, after the market has exited. A failed commit is logged, never
   raised into a request.
4. The web app gets `BAZAAR_MARKET_URL=http://127.0.0.1:8000`, `BAZAAR_RUNS_DIR=/data/runs`,
   `BAZAAR_WEB_DB=/data/web.sqlite3` and `BAZAAR_MARKET_DB=/data/market.sqlite3`.

The scored-run hook: before serving, `modal_app.py` sets `app.state.on_scored` on `bazaar_web.app:app` to a callable
`on_scored(submission_id, run_dir)`. The web worker should call `request.app.state.on_scored(...)`, or
`app.state.on_scored(...)` from its lifespan worker, right after it writes a scored run. Outside Modal the attribute is
absent, so the web app should treat it as optional: `getattr(app.state, "on_scored", None)`.

Known limit: a commit can snapshot SQLite between two transactions. On restart SQLite recovers from the committed WAL
file, so the worst case loses up to 10 s of writes.

## Redeploys

Read this before any second deploy. A Modal Volume keeps the last write of each file. During a redeploy the old
container's final commit can land after the new container has started. That can drop the new container's writes, or
pair the main market database with a `-wal` file from the other container, which corrupts it. So:

1. **Do the `PUBLIC_URL` redeploy before the QR code goes up.** The first deploy prints the web URL. Add it to the
   `bazaar-live` secret as `PUBLIC_URL`, redeploy, then show the QR code. Nobody has submitted yet, so nothing is lost.
2. **Mid-event, redeploy only with an empty queue.** Check that no submission is queued or running:

   ```sh
   curl -s "$PUBLIC_URL/api/board" \
     | python3 -c "import json,sys; print(sum(r['status'] in ('queued','running') for r in json.load(sys.stdin)['rows']))"
   ```

   Redeploy only when it prints `0`. If needed, close the form first and wait for the queue to drain.

### Reseeding the market database

A new `BAZAAR_DEPLOY_MARKET_DB` never replaces a database already on the volume: the startup log says
`already on the volume; seed not applied`. To reseed deliberately, which loses every account and order in it:

```sh
uvx modal app stop bazaar-live
uvx modal volume rm bazaar-live-data /market.sqlite3
uvx modal volume rm bazaar-live-data /market.sqlite3-wal   # if listed by: uvx modal volume ls bazaar-live-data
uvx modal volume rm bazaar-live-data /market.sqlite3-shm   # likewise
BAZAAR_DEPLOY_MARKET_DB=... BAZAAR_DEPLOY_RUNS=... BAZAAR_DEPLOY_FONT=... uvx modal deploy deploy/modal_app.py
```

The startup log then says `freshly seeded from /app/seed/market.sqlite3`. For a new set of seed runs, use the
one-shot reseed below, which archives instead of deleting.

### Reseed runs (one-shot)

For the v2 switch: replace the board's runs with a new seed set, keeping the old ones in an archive.

1. Stage the new seed runs in a local folder: one folder per run, each with `record.json` and `evaluation.json`.
2. Check that the queue is empty (see the Redeploys rules above).
3. Stop the app first, so the old container's last volume commit cannot write old run files onto the new board:

   ```sh
   uvx modal app stop -y bazaar-live
   ```

4. Deploy with a new reseed id:

   ```sh
   BAZAAR_RESEED_RUNS=v2-2026-10-08 \
   BAZAAR_DEPLOY_RUNS=/path/to/new-seed-runs \
   BAZAAR_DEPLOY_MARKET_DB=... BAZAAR_DEPLOY_FONT=... \
     uvx modal deploy deploy/modal_app.py
   ```

On start the container checks the seed, copies the new seed runs, moves `/data/runs` to
`/data/archive/<utc time>/runs`, puts the new runs in place, writes the marker `/data/archive/reseeded-<id>`, and
commits the volume. The log says what it did: `archived the previous runs to ...` and `copied N seed runs`, or
`skipped: already done`.

- **One-shot.** The id stays in the deploy's environment, and the container restarts if the market dies. The marker
  means a given id reseeds once: a restart logs `skipped` and leaves runs scored since then alone. To reseed again
  later, deploy with a new id. A later deploy without `BAZAAR_RESEED_RUNS` does nothing.
- **Nothing is deleted.** Every earlier board stays under `/data/archive/`.
- **Fail closed.** With the id set, the seed is checked before anything moves. Dotfiles such as `.DS_Store` are
  ignored and not copied. If the seed is missing, holds no runs, or has any other entry that is not a folder with
  both `record.json` and `evaluation.json`, the container fails to start, the log names the bad entries, and the
  board is untouched.
- **Not touched:** `web.sqlite3` and its submission rows. The PM reruns the attendee strategies through the admin route.

**Recovery.** A bad seed makes the container fail on every start, so Modal keeps restarting it. To get the old board
back up:

1. Redeploy **without** `BAZAAR_RESEED_RUNS`, using the normal deploy command above. Nothing moved, so the board
   comes back as it was. Fix the seed folder, then reseed with a new id.
2. If a reseed did run and its runs are wrong, stop the app with `uvx modal app stop -y bazaar-live`. Then find the
   archive with `uvx modal volume ls bazaar-live-data /archive`. modal 1.6.1 has no `volume mv`, so archive the
   current runs by copying them first, since they may include attendee runs scored after the reseed. Check that the
   copy is listed, and only then delete and copy the old runs back:

   ```sh
   uvx modal volume cp -r bazaar-live-data /runs /archive/<now utc time>-replaced/runs
   uvx modal volume ls bazaar-live-data /archive/<now utc time>-replaced/runs   # must list the same runs as /runs
   uvx modal volume rm -r bazaar-live-data /runs
   uvx modal volume cp -r bazaar-live-data /archive/<utc time>/runs /runs
   ```

   Then redeploy without `BAZAAR_RESEED_RUNS`. The `reseeded-<id>` marker stays, so that id never runs again.

## Filling the board

Two scripts submit house strategies to a running deploy, so the board has something on it before
and between attendees. Both submit under the handle `house` (change it with `--handle`), take the
deploy URL as an argument or the `BASE_URL` environment variable, and need no token.

- `scripts/house_strategies.py` submits a fixed batch of 30 varied strategies once. Use it to seed
  an empty board.

  ```bash
  uv run python scripts/house_strategies.py BASE_URL [--handle house] [--only N]
  ```

- `scripts/house_feeder.py` keeps the board busy without crowding out attendees. It submits a new
  generated strategy whenever fewer than `--depth` submissions are queued or running, and stops
  after `--max` have been queued. Use it while the board is on screen.

  ```bash
  uv run python scripts/house_feeder.py BASE_URL [--depth 4] [--max 80] [--handle house]
  ```

Each run cost about $0.26 to $1.28 in model calls on the live deploy (observed in October 2026). House runs count
against the same caps as attendees: the queue cap, the daily cap and the per-IP hourly cap all answer 429. With the
default per-IP cap of 5 an hour, 30 strategies from one address take about 6 hours. On 429 or 503,
`house_strategies.py` waits 60 seconds and retries the same name. On 429, `house_feeder.py` waits 5 minutes and then
moves on to the next generated name; the rate-limited name is dropped. A name that is already taken is skipped. Stop
either script with Ctrl-C; submissions already queued still run. The submit page's link to a strategy's Logfire
dashboard comes from `BAZAAR_LOGFIRE_DASHBOARD_URL`.

## Logfire

Runs export to the `bazaar-demo` Logfire project when `LOGFIRE_TOKEN`, a project write token, is set. On Modal it
comes from the `bazaar-live` secret above. Every process configures Logfire with `send_to_logfire="if-token-present"`,
so nothing is sent without it. `docs/telemetry.md` lists the spans and attributes.

## Trust boundaries

The boundaries are the point of the demo:

- **The agent never moves the clock.** Only the runner holds `X-Bazaar-Runner-Token`, which the market requires on
  its control routes (`ledger_api.py`, `grants.py`). The agent's market client carries only `X-Bazaar-Approval` and
  `X-Bazaar-Account` (`runner/agent_step.py`).
- **The agent never sees prices after the cutoff.** The market answers 403 for prices, news or filings past the
  experiment's current time (`prices_api.py`, `news_api.py`, `filings_api.py`).
- **The agent never changes a balance except by placing an order.** Cash changes only when an order fills; opening
  and closing accounts are runner-only control routes.
- **The agent never sees its scores.** None of its tools returns a score, and the strategy judge runs after the
  decision and writes only to spans (`agent/strategy_evaluation.py`, applied in `agent/trading.py`).

## Open questions for Duncan

1. Account on news and filings: the research routes take the account from the `X-Bazaar-Account` header. Keep the
   header, or add the account to the route?
2. Fiscal cycles: a cycle starts the day after the latest 10-K/10-Q period end accepted by the cutoff, so in effect a
   filing is visible from its acceptance. "Prior fiscal year only" would be a stricter rule that you would define.
   Which do you want?
3. `ResearchTools` pins identical queries, and nothing clears the pins after an order, so `orders()` after
   `market_order()` in one decision fails with `invalid_response` (the history legitimately changed). The runner
   reconciles it, but should the pins be cleared after an order?
4. Market approvals are bound to the experiment, not the account (`grants.py`), so they are safe only with one
   agent per experiment. #18 should issue account-scoped credentials.

## Modal APIs used, checked against modal 1.6.1

Signatures printed from the installed package with `inspect.signature`:

- `Image.debian_slim(python_version=None, force_build=False)`
- `Image.pip_install(*packages, ..., env=None, secrets=None, gpu=None)`
- `Image.add_local_file(local_path, remote_path, *, copy=False)`
- `Image.add_local_dir(local_path, remote_path, *, copy=False, ignore=[])`. `copy=True` is required before a later build step.
- `Image.run_commands(*commands, env=None, secrets=None, volumes=None, gpu=None, force_build=False)`
- `Image.env(vars)`
- `Volume.from_name(name, *, environment_name=None, create_if_missing=False, ...)` and `Volume.commit()`
- `Secret.from_name(name, *, environment_name=None, required_keys=[], ...)`
- `App(name=None, *, tags=None, image=None, secrets=[], volumes={}, include_source=True)`
- `App.cls(*, image, env, secrets, volumes, min_containers, max_containers, timeout=300, ...)`
- `modal.enter(*, snap=False)`, `modal.exit()`, `modal.asgi_app(*, label=None, ...)`, `modal.concurrent(*, max_inputs=None, target_inputs=None)`
- `modal.is_local()`: False only inside a running Modal Function, so the deploy-time paths are read only locally.
- CLI, from `uvx modal ... --help`: `modal app stop [-y] APP_IDENTIFIER`, `modal volume ls VOLUME_NAME [PATH]`,
  `modal volume rm [-r] VOLUME_NAME REMOTE_PATH`, `modal volume cp [-r] VOLUME_NAME PATHS...`.

`Image.uv_sync` is not used: modal 1.6.1 raises `uv workspaces are not supported` for a `pyproject.toml` with
`[tool.uv.workspace]`, which ours has. The `run_commands` sync above does the same job.

Checks done without deploying: `python -m py_compile` passes, and `modal_app.py` imports under
`uvx --from modal python` with stand-in paths (app `bazaar-live`, class `Live` registered). The system-environment
sync was checked locally with `UV_PROJECT_ENVIRONMENT=<tmp> uv sync --frozen --no-dev --all-packages --inexact`,
followed by importing `bazaar_market.app` with that interpreter. Not checked: an actual Modal image build and
container start, which need a deploy.

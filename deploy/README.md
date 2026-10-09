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

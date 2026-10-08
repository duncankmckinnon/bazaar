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

The Modal secret `bazaar-live`, which Anthony creates. Nobody prints its values. The deploy requires the first two keys:

- `BAZAAR_RUNNER_TOKEN`
- `PYDANTIC_AI_GATEWAY_API_KEY`
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
   the seed DB to `/data/market.sqlite3` and the seed runs to `/data/runs` only if they are absent, then starts the
   market and waits up to 30 s for its `/health`. If the market does not come up, the container fails to start.
3. The Volume `bazaar-live-data` at `/data` holds `market.sqlite3`, `runs/` and `web.sqlite3`. It is committed every
   10 s, after each scored run, and once at shutdown. A failed commit is logged, never raised into a request.
4. The web app gets `BAZAAR_MARKET_URL=http://127.0.0.1:8000`, `BAZAAR_RUNS_DIR=/data/runs`,
   `BAZAAR_WEB_DB=/data/web.sqlite3` and `BAZAAR_MARKET_DB=/data/market.sqlite3`.

The scored-run hook: before serving, `modal_app.py` sets `app.state.on_scored` on `bazaar_web.app:app` to a callable
`on_scored(submission_id, run_dir)`. The web worker should call `request.app.state.on_scored(...)`, or
`app.state.on_scored(...)` from its lifespan worker, right after it writes a scored run. Outside Modal the attribute is
absent, so the web app should treat it as optional: `getattr(app.state, "on_scored", None)`.

Known limit: a commit can snapshot SQLite between two transactions. On restart SQLite recovers from the committed WAL
file, so the worst case loses up to 10 s of writes.

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

`Image.uv_sync` is not used: modal 1.6.1 raises `uv workspaces are not supported` for a `pyproject.toml` with
`[tool.uv.workspace]`, which ours has. The `run_commands` sync above does the same job.

Checks done without deploying: `python -m py_compile` passes, and `modal_app.py` imports under
`uvx --from modal python` with stand-in paths (app `bazaar-live`, class `Live` registered). The system-environment
sync was checked locally with `UV_PROJECT_ENVIRONMENT=<tmp> uv sync --frozen --no-dev --all-packages --inexact`,
followed by importing `bazaar_market.app` with that interpreter. Not checked: an actual Modal image build and
container start, which need a deploy.

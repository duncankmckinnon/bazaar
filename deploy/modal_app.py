"""The conference app on Modal: the web app on a public URL, the market beside it.

Deploy (PM only; needs the three paths below, which are never committed):

    BAZAAR_MODAL_TARGET=live BAZAAR_DEPLOY_MARKET_DB=... BAZAAR_DEPLOY_FONT=... \\
        BAZAAR_DEPLOY_RUNS=... uvx modal deploy deploy/modal_app.py

BAZAAR_MODAL_TARGET is required: live, or staging for the separate load-test app (see
deploy/README.md).
One container (min and max 1) runs the market on 127.0.0.1:8000 as a subprocess and serves the
web app. A Volume at /data keeps runs, the web database and a writable copy of the market
database, committed every 10 seconds and after each scored run. See deploy/README.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import modal

# live_runtime.py sits beside this file locally and in /root in the container.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_runtime

# BAZAAR_MODAL_TARGET (required) picks live or staging. See deploy/README.md.
TARGET = live_runtime.resolve_target()
CPU, MEMORY_MIB = live_runtime.resolve_resources()
APP_NAME = TARGET.app
if modal.is_local():
    print(
        f"deploying target={TARGET.name} app={TARGET.app} volume={TARGET.volume} "
        f"secret={TARGET.secret} cpu={CPU:g} memory={MEMORY_MIB}MiB",
        file=sys.stderr,
    )
REPO = Path(__file__).resolve().parents[1]
DATA = "/data"
SEED_DB = "/app/seed/market.sqlite3"
SEED_RUNS = "/app/seed/runs"
FONTS_DIR = "/app/fonts"
MARKET_URL = "http://127.0.0.1:8000"
# Never send local environments, secrets or data into the image.
SOURCE_IGNORE = [
    "**/.venv",
    "**/__pycache__",
    "**/.env*",
    "**/*.sqlite3*",
    "**/data",
    "**/.pytest_cache",
]


def _deploy_path(name: str) -> str:
    value = os.environ.get(name)
    if not value or not Path(value).exists():
        raise RuntimeError(f"set {name} to an existing local path before deploying")
    return value


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("uv")
    .add_local_file(REPO / "pyproject.toml", "/app/pyproject.toml", copy=True)
    .add_local_file(REPO / "uv.lock", "/app/uv.lock", copy=True)
    .add_local_dir(REPO / "packages", "/app/packages", copy=True, ignore=SOURCE_IGNORE)
    .add_local_dir(REPO / "services", "/app/services", copy=True, ignore=SOURCE_IGNORE)
    # Modal runs functions with the image's system Python, so install the workspace there.
    # --inexact keeps the packages Modal's own runtime needs.
    .run_commands(
        "cd /app && uv sync --frozen --no-dev --all-packages --inexact",
        env={"UV_PROJECT_ENVIRONMENT": "/usr/local"},
    )
    .env({"BAZAAR_FONTS_DIR": FONTS_DIR})
)
if modal.is_local():
    # Data and fonts come from local paths at deploy time and are added at container start.
    image = (
        image.add_local_file(_deploy_path("BAZAAR_DEPLOY_MARKET_DB"), SEED_DB)
        .add_local_dir(_deploy_path("BAZAAR_DEPLOY_RUNS"), SEED_RUNS)
        .add_local_file(_deploy_path("BAZAAR_DEPLOY_FONT"), f"{FONTS_DIR}/DwightMedium.woff2")
        .add_local_file(Path(__file__).with_name("live_runtime.py"), "/root/live_runtime.py")
    )

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(TARGET.volume, create_if_missing=True)
secret = modal.Secret.from_name(
    TARGET.secret,
    required_keys=[
        "BAZAAR_RUNNER_TOKEN",
        "PYDANTIC_AI_GATEWAY_API_KEY",
        "LOGFIRE_API_KEY",
    ],
)
WEB_ENV = {
    # The market runs as a subprocess of the web app and inherits this environment.
    "BAZAAR_ENVIRONMENT": TARGET.environment,
    # The container imports this file again; it must resolve the same target and volume.
    "BAZAAR_MODAL_TARGET": TARGET.name,
    "BAZAAR_MARKET_URL": MARKET_URL,
    "BAZAAR_MARKET_DB": f"{DATA}/market.sqlite3",
    "BAZAAR_RUNS_DIR": f"{DATA}/runs",
    "BAZAAR_WEB_DB": f"{DATA}/web.sqlite3",
    **TARGET.extra_env,
}
if modal.is_local() and os.environ.get("BAZAAR_RESEED_RUNS", "").strip():
    # One-shot reseed of the board's runs for this deploy. See deploy/README.md.
    WEB_ENV["BAZAAR_RESEED_RUNS"] = os.environ["BAZAAR_RESEED_RUNS"].strip()
if modal.is_local() and os.environ.get("BAZAAR_MAX_CONCURRENT", "").strip():
    # The web worker's run concurrency. Unset, the web app keeps its own default.
    WEB_ENV["BAZAAR_MAX_CONCURRENT"] = os.environ["BAZAAR_MAX_CONCURRENT"].strip()


@app.cls(
    image=image,
    volumes={DATA: volume},
    secrets=[secret],
    env=WEB_ENV,
    cpu=CPU,
    memory=MEMORY_MIB,
    min_containers=1,
    max_containers=1,
    timeout=3600,
)
@modal.concurrent(max_inputs=200)
class Live:
    @modal.enter()
    def start(self) -> None:
        import logging
        import threading

        logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
        live_runtime.require_runner_token()
        live_runtime.reseed_runs(
            Path(DATA), Path(SEED_RUNS), os.environ.get("BAZAAR_RESEED_RUNS"), commit=volume.commit
        )
        db, _ = live_runtime.prepare_data(Path(DATA), Path(SEED_DB), Path(SEED_RUNS))
        self.committer = live_runtime.VolumeCommitter(volume.commit).start()
        self.committer.commit_now("seed")
        self.market = live_runtime.start_market(db)
        live_runtime.wait_healthy(f"{MARKET_URL}/health", timeout=30, process=self.market)
        self.stopping = threading.Event()
        live_runtime.watch_market(
            self.market,
            self.stopping,
            before_exit=lambda: self.committer.commit_now("market exited"),
        )

    @modal.exit()
    def stop(self) -> None:
        live_runtime.shutdown(self.market, self.committer, self.stopping)

    @modal.asgi_app()
    def web(self):
        from bazaar_web.app import app as web_app

        # The web worker calls this after each scored run, so the result survives a restart.
        web_app.state.on_scored = self.committer.on_scored
        return web_app

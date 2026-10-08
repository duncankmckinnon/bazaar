"""deploy/entrypoint.sh: one container running the market (internal) and the web app (public).

Runs the real script with the real market app and a stand-in web app on free ports, from a
temporary seed. Never touches data/.
"""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[3] / "deploy" / "entrypoint.sh"
TOKEN = "dummy-runner-token-do-not-print"
WEB_APP = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"web": "ok"}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def box(tmp_path):
    """A seed folder, a stand-in web app module and an environment for the script."""
    seed = tmp_path / "seed"
    (seed / "runs" / "baseline").mkdir(parents=True)
    (seed / "runs" / "baseline" / "run.json").write_text("{}")
    with closing(sqlite3.connect(seed / "market.sqlite3")) as connection:
        connection.execute("CREATE TABLE marker (x)")
        connection.commit()
    (tmp_path / "standin_web.py").write_text(WEB_APP)
    (tmp_path / "broken_web.py").write_text("raise RuntimeError('the web app failed to import')\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BAZAAR_", "LOGFIRE_", "PORT"))}
    env |= {
        "PATH": f"{Path(sys.executable).parent}:{env['PATH']}",
        "PYTHONPATH": str(tmp_path),
        "BAZAAR_RUNNER_TOKEN": TOKEN,
        "BAZAAR_SEED_DIR": str(seed),
        "BAZAAR_MARKET_DB": str(tmp_path / "data" / "market.sqlite3"),
        "BAZAAR_RUNS_DIR": str(tmp_path / "data" / "runs"),
        "BAZAAR_MARKET_PORT": str(free_port()),
        "BAZAAR_WEB_APP": "standin_web:app",
        "PORT": str(free_port()),
    }
    return tmp_path, env


def start(env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        ["bash", str(ENTRYPOINT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def get(port: str, path: str = "/health") -> bytes | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
            return response.read()
    except OSError:
        return None


def wait_until_up(process: subprocess.Popen, port: str, timeout: float = 30) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, process.communicate()
        body = get(port)
        if body is not None:
            return body
        time.sleep(0.2)
    raise AssertionError(f"nothing answered on port {port}")


def stop(process: subprocess.Popen) -> tuple[int, str]:
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=20)
    return process.returncode, (out + err).decode()


def test_both_services_start_from_the_seed_and_stop_cleanly(box):
    tmp_path, env = box
    process = start(env)
    try:
        assert wait_until_up(process, env["PORT"]) == b'{"web":"ok"}'
        assert get(env["BAZAAR_MARKET_PORT"]) == b'{"status":"ok"}'
        with closing(sqlite3.connect(tmp_path / "data" / "market.sqlite3")) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "marker" in tables  # the seed was copied, then the market added its schema
        assert (tmp_path / "data" / "runs" / "baseline" / "run.json").exists()
    finally:
        code, output = stop(process)
    assert code == 0
    assert TOKEN not in output


def test_an_existing_database_is_kept_not_overwritten(box):
    tmp_path, env = box
    (tmp_path / "data").mkdir()
    with closing(sqlite3.connect(tmp_path / "data" / "market.sqlite3")) as connection:
        connection.execute("CREATE TABLE kept (x)")
        connection.commit()
    process = start(env)
    try:
        wait_until_up(process, env["PORT"])
        with closing(sqlite3.connect(tmp_path / "data" / "market.sqlite3")) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "kept" in tables and "marker" not in tables
    finally:
        stop(process)


def test_without_a_runner_token_it_refuses_to_start(box):
    _, env = box
    del env["BAZAAR_RUNNER_TOKEN"]

    result = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=env, capture_output=True, timeout=20, check=False
    )

    assert result.returncode != 0
    assert b"BAZAAR_RUNNER_TOKEN is not set" in result.stderr


def test_without_a_seed_or_database_it_refuses_to_start(box):
    tmp_path, env = box
    env["BAZAAR_SEED_DIR"] = str(tmp_path / "no-seed")

    result = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=env, capture_output=True, timeout=20, check=False
    )

    assert result.returncode != 0
    assert b"no seed" in result.stderr


def test_the_container_exits_non_zero_when_the_web_app_dies(box):
    _, env = box
    env["BAZAAR_WEB_APP"] = "broken_web:app"
    process = start(env)

    out, err = process.communicate(timeout=60)

    assert process.returncode != 0
    assert b"a process exited" in err
    assert get(env["BAZAAR_MARKET_PORT"]) is None  # the market was stopped too
    assert TOKEN.encode() not in out + err

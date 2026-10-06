"""Dry run of the runner's path against a real market process, over HTTP.

    uv run python scripts/market-dry-run.py
    uv run python scripts/market-dry-run.py --db data/market.sqlite3 --data-version alpaca-bars-v1

It starts uvicorn on a free port with a temporary database, then makes the runner's calls in
order and prints one line per call. Without --db it imports synthetic-v1 first. With --db it
copies that database and runs against the copy, so the original is never written. Logfire is
off. Exits 1 at the first unexpected response.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

EASTERN = ZoneInfo("America/New_York")
SESSIONS = [d for d in (date(2026, 2, 2) + timedelta(days=n) for n in range(12)) if d.weekday() < 5]
SELL_ON = date(2026, 2, 9)
RUNNER_TOKEN = "dry-run-runner-token"


class Unexpected(Exception):
    pass


def close_z(day: date) -> str:
    close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=EASTERN).astimezone(UTC)
    return close.strftime("%Y-%m-%dT%H:%M:%SZ")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Run:
    def __init__(self, client: httpx.Client, experiment: uuid.UUID, approval: uuid.UUID) -> None:
        self.client = client
        self.experiment = experiment
        self.approval = approval

    def call(
        self,
        label: str,
        method: str,
        path: str,
        status: int,
        *,
        json: dict | None = None,
        params: dict | None = None,
        runner: bool = False,
        approval: uuid.UUID | None = None,
        code: str | None = None,
    ) -> dict:
        headers = {"X-Bazaar-Approval": str(approval or self.approval)}
        if runner:
            headers["X-Bazaar-Runner-Token"] = RUNNER_TOKEN
        url = f"/experiments/{self.experiment}{path}"
        response = self.client.request(method, url, headers=headers, json=json, params=params)
        body = response.json() if response.content else {}
        got_code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
        ok = response.status_code == status and (code is None or got_code == code)
        print(
            f"{'ok ' if ok else 'BAD'} {response.status_code} {method:4} {label}: {summary(body)}"
        )
        if not ok:
            raise Unexpected(
                f"{label}: expected {status}{' ' + code if code else ''}, got "
                f"{response.status_code} {response.text}"
            )
        return body

    def check_fill(self, fill: dict, cutoff: str) -> None:
        """The fill is priced at the close the price route shows at this cutoff, never later."""
        label = f"{fill.get('side')} {fill.get('symbol')}"
        self.expect(f"{label} filled", fill.get("status") == "filled", str(fill.get("status")))
        observed = datetime.fromisoformat(fill["price_observed_at"])
        self.expect(
            f"{label} price observed by the cutoff",
            observed <= datetime.fromisoformat(cutoff),
            f"price_observed_at {fill['price_observed_at']} <= cutoff {cutoff}",
        )
        start = (datetime.fromisoformat(cutoff) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        history = self.call(
            f"close of {fill['symbol']} at {cutoff}",
            "GET",
            f"/prices/{fill['symbol']}",
            200,
            params={"start_at": start, "end_at": cutoff},
        )
        latest = history["observations"][-1]
        self.expect(
            f"{label} price is that close",
            Decimal(fill["unit_price"]) == Decimal(latest["price"])
            and fill["price_observed_at"] == latest["observed_at"],
            f"unit_price {fill['unit_price']} vs close {latest['price']} "
            f"observed {latest['observed_at']}",
        )

    def expect(self, label: str, condition: bool, detail: str) -> None:
        print(f"{'ok ' if condition else 'BAD'}     check {label}: {detail}")
        if not condition:
            raise Unexpected(f"{label}: {detail}")


def summary(body: dict) -> str:
    if not isinstance(body, dict):
        return str(body)[:120]
    if "error" in body:
        return f"error={body['error'].get('code')} {body['error'].get('message')}"
    if body.get("status") == "rejected":
        return (
            f"rejected {body['side']} {body['quantity']} {body['symbol']}: {body['error']['code']}"
        )
    if body.get("status") == "filled":
        return (
            f"filled {body['side']} {body['quantity']} {body['symbol']} @ {body['unit_price']} "
            f"(close of {body['price_observed_at']}) order={body['order_id'][:8]} "
            f"cash={body['account']['cash']}"
        )
    if "portfolio_value" in body:
        holdings = ", ".join(
            f"{h['quantity']} {h['symbol']}@{h['unit_mark']}" for h in body["holdings"]
        )
        return f"value={body['portfolio_value']} cash={body['cash']} [{holdings}]"
    if "observations" in body:
        return f"{len(body['observations'])} observations, cutoff={body['cutoff_at']}"
    if "cash" in body:
        return f"account={body['account_id'][:8]} cash={body['cash']} state={body['state_version']}"
    return ", ".join(f"{k}={v}" for k, v in body.items())[:160]


def dry_run(run: Run, data_version: str, db: Path) -> None:
    first = SESSIONS[0]
    cutoff_body = {
        "cutoff": close_z(first),
        "data_version": data_version,
        "execution_rule_version": "exec-v1",
    }

    # A launch with an approval that is not on the list is refused and writes nothing.
    run.call(
        "denied launch: cutoff with an unlisted approval",
        "PUT",
        "/cutoff",
        403,
        json=cutoff_body,
        runner=True,
        approval=uuid.uuid4(),
        code="experiment_not_approved",
    )
    run.call(
        "after the denied launch, the experiment has no clock",
        "GET",
        "/prices/AAPL",
        404,
        params={"start_at": close_z(first), "end_at": close_z(first)},
    )
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM acct_experiments").fetchone()[0]
    run.expect("denied launch wrote nothing", rows == 0, f"acct_experiments rows={rows}")

    run.call(
        f"launch: cutoff {cutoff_body['cutoff']}",
        "PUT",
        "/cutoff",
        200,
        json=cutoff_body,
        runner=True,
    )
    account = run.call(
        "create account with 100000",
        "POST",
        "/accounts",
        201,
        json={
            "request_id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "strategy_version_id": str(uuid.uuid4()),
            "cash": "100000",
        },
        runner=True,
    )
    account_path = f"/accounts/{account['account_id']}"

    def order(side: str, symbol: str, client_order_id: str | None = None) -> dict:
        return {
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "symbol": symbol,
            "side": side,
            "quantity": "10",
        }

    buy_aapl = run.call(
        "BUY 10 AAPL", "POST", f"{account_path}/orders", 200, json=order("buy", "AAPL")
    )
    run.check_fill(buy_aapl, close_z(first))
    ko_order = order("buy", "KO")
    buy_ko = run.call("BUY 10 KO", "POST", f"{account_path}/orders", 200, json=ko_order)
    run.check_fill(buy_ko, close_z(first))

    for day in SESSIONS:
        if day != first:
            run.call(
                f"advance cutoff to {close_z(day)}",
                "PUT",
                "/cutoff",
                200,
                json={"cutoff": close_z(day)},
                runner=True,
            )
        portfolio = run.call(f"portfolio at {day}", "GET", f"{account_path}/portfolio", 200)
        marked = sum(
            Decimal(h["quantity"]) * Decimal(h["unit_mark"]) for h in portfolio["holdings"]
        )
        total = Decimal(portfolio["cash"]) + marked
        run.expect(
            f"value adds up on {day}",
            total == Decimal(portfolio["portfolio_value"]),
            f"cash + marks = {total}, portfolio_value = {portfolio['portfolio_value']}",
        )
        cutoff = datetime.fromisoformat(close_z(day))
        late = [
            h["symbol"]
            for h in portfolio["holdings"]
            if datetime.fromisoformat(h["mark_available_at"]) > cutoff
        ]
        run.expect(f"no mark after the cutoff on {day}", not late, f"late marks: {late or 'none'}")

        if day == SELL_ON:
            sell = run.call(
                "SELL 10 AAPL", "POST", f"{account_path}/orders", 200, json=order("sell", "AAPL")
            )
            run.check_fill(sell, close_z(day))
            oversell = run.call(
                "oversell: SELL 10 AAPL again",
                "POST",
                f"{account_path}/orders",
                200,
                json=order("sell", "AAPL"),
            )
            code = (oversell.get("error") or {}).get("code")
            run.expect(
                "oversell rejected",
                oversell.get("status") == "rejected" and code == "insufficient_holdings",
                f"status={oversell.get('status')} code={code}",
            )
            before = run.call("account before the retry", "GET", account_path, 200)
            retry = run.call(
                "retry the KO buy with its client_order_id",
                "POST",
                f"{account_path}/orders",
                200,
                json=ko_order,
            )
            after = run.call("account after the retry", "GET", account_path, 200)
            run.expect(
                "retry returned the original order",
                retry.get("order_id") == buy_ko["order_id"],
                f"{retry.get('order_id')} vs {buy_ko['order_id']}",
            )
            run.expect(
                "retry did not debit again",
                (after["cash"], after["state_version"])
                == (before["cash"], before["state_version"]),
                f"cash {before['cash']} -> {after['cash']}, "
                f"state {before['state_version']} -> {after['state_version']}",
            )

    last = SESSIONS[-1]
    future = (datetime.fromisoformat(close_z(last)) + timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    run.call(
        "prices with end_at 1 s past the cutoff",
        "GET",
        "/prices/AAPL",
        403,
        params={"start_at": close_z(first), "end_at": future},
        code="forbidden",
    )
    run.call(
        "prices up to the cutoff",
        "GET",
        "/prices/AAPL",
        200,
        params={"start_at": close_z(first), "end_at": close_z(last)},
    )
    run.call("close the account", "POST", f"{account_path}/close", 200, runner=True)
    run.call("order after close", "POST", f"{account_path}/orders", 409, json=order("buy", "KO"))


def prepare_db(args: argparse.Namespace, workdir: Path) -> Path:
    db = workdir / "market.sqlite3"
    if args.db:
        # The market database is WAL, so a file copy can miss commits still in the -wal file.
        with closing(sqlite3.connect(args.db)) as source, closing(sqlite3.connect(db)) as copy:
            source.backup(copy)
        print(f"using a copy of {args.db} with data version {args.data_version}")
        return db
    csv_path = workdir / "bars-synthetic-v1.csv"
    for command in (
        ["synthetic", "--out", str(csv_path)],
        ["import", str(csv_path), "--db", str(db)],
    ):
        subprocess.run([sys.executable, "-m", "bazaar_market.prices", *command], check=True)
    return db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, help="an existing market database to copy")
    parser.add_argument("--data-version", default="synthetic-v1")
    args = parser.parse_args()
    if args.db and not args.db.is_file():
        parser.error(f"--db {args.db} does not exist")
    if args.db and args.data_version == "synthetic-v1":
        parser.error("with --db, name the imported --data-version, e.g. alpaca-bars-v1")

    workdir = Path(tempfile.mkdtemp(prefix="market-dry-run-"))
    db = prepare_db(args, workdir)
    experiment, approval = uuid.uuid4(), uuid.uuid4()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGFIRE_")}
    env |= {
        "BAZAAR_MARKET_DB": str(db),
        "BAZAAR_DEV_APPROVAL_IDS": f"{approval}:{experiment}",
        "BAZAAR_RUNNER_TOKEN": RUNNER_TOKEN,
        "LOGFIRE_SEND_TO_LOGFIRE": "false",
    }
    port = free_port()
    log = (workdir / "server.log").open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bazaar_market.app:app", "--port", str(port)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            for _ in range(100):
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if server.poll() is not None:
                    raise Unexpected(f"the server exited with {server.returncode}")
                time.sleep(0.1)
            else:
                raise Unexpected("the server did not answer /health within 10 s")
            print(f"market on port {port}, experiment {experiment}, approval {approval}")
            dry_run(Run(client, experiment, approval), args.data_version, db)
    except (Unexpected, httpx.HTTPError, KeyError, ValueError) as failure:
        log.flush()
        print(f"\nDRY RUN FAILED: {failure!r}", file=sys.stderr)
        print(f"server log: {workdir / 'server.log'}", file=sys.stderr)
        return 1
    finally:
        server.terminate()
        server.wait(timeout=10)
        log.close()
    print("\nDRY RUN PASSED")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

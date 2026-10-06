"""Pure Monty calculations over runner-owned historical snapshots, without artificial quotas."""

import asyncio
import hashlib
import json
import sys
import time
from typing import Literal, Self
from uuid import UUID, uuid4

import logfire
from bazaar_protocol import (
    AccountSnapshot,
    ExperimentContext,
    PortfolioSnapshot,
    PriceHistory,
    WireModel,
)
from pydantic import model_validator

from bazaar_agent.research import ToolError, ToolResult

Status = Literal["ok", "denied", "syntax", "runtime", "serialization", "worker_error", "cancelled"]


class CalculationSnapshot(WireModel):
    """Runner-owned inputs; never model-callable arguments or evaluator data."""

    context: ExperimentContext
    prices: tuple[PriceHistory, ...] = ()
    account: AccountSnapshot | None = None
    portfolio: PortfolioSnapshot | None = None

    @model_validator(mode="after")
    def cutoff_safe(self) -> Self:
        ctx = self.context
        if len({history.symbol for history in self.prices}) != len(self.prices):
            raise ValueError("Duplicate symbols")
        for history in self.prices:
            if (
                history.experiment_id != ctx.experiment_id
                or history.data_version != ctx.data_version
                or history.cutoff_at > ctx.simulated_at
            ):
                raise ValueError("Price history outside runner scope/cutoff")
        for snapshot in (self.account, self.portfolio):
            if snapshot is None:
                continue
            if (
                snapshot.experiment_id != ctx.experiment_id
                or snapshot.account_id != ctx.account_id
                or snapshot.simulated_at > ctx.simulated_at
            ):
                raise ValueError("Snapshot outside runner scope/cutoff")
            if isinstance(snapshot, AccountSnapshot) and (
                snapshot.agent_id != ctx.agent_id
                or snapshot.strategy_version_id != ctx.strategy_version_id
            ):
                raise ValueError("Account identity mismatch")
            if (
                isinstance(snapshot, PortfolioSnapshot)
                and snapshot.data_version != ctx.data_version
            ):
                raise ValueError("Portfolio version mismatch")
        if self.account is not None and self.portfolio is not None:
            if (self.account.simulated_at, self.account.state_version, self.account.cash) != (
                self.portfolio.simulated_at,
                self.portfolio.state_version,
                self.portfolio.cash,
            ):
                raise ValueError("Account and portfolio snapshots disagree")
            if {h.symbol: h.quantity for h in self.account.holdings} != {
                h.symbol: h.quantity for h in self.portfolio.holdings
            }:
                raise ValueError("Account and portfolio holdings disagree")
        return self


class CalculationRecord(WireModel):
    execution_id: UUID
    context: ExperimentContext
    sdk_version: Literal["0.0.14"] = "0.0.14"
    code: str
    code_digest: str
    inputs_json: str
    snapshot_digest: str
    status: Status
    output_json: str | None = None
    error_text: str | None = None
    prints_json: str = "[]"
    duration_seconds: float


def encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode(errors="surrogatepass")).hexdigest()


async def _reap(creation, process, communication) -> None:
    try:
        if creation is not None and process is None:
            process = await creation
    except Exception:  # noqa: BLE001 -- spawn failed; no process to reap
        return
    if process is not None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if communication is not None:
            try:
                # Keep pipe readers alive through kill: wait() alone can deadlock
                # when an abandoned reader has paused a full output transport.
                await communication
            except Exception:  # noqa: BLE001, S110 -- private failure already handled by caller
                pass
        await process.wait()


class MontyCalculator:
    """A fresh runner-bound snapshot per decision; all executions use fresh workers."""

    def __init__(self, snapshot: CalculationSnapshot) -> None:
        self.records: list[CalculationRecord] = []
        self._reserved = False
        # Frozen objects can still be forged with model_construct/model_copy.
        self.snapshot = CalculationSnapshot.model_validate_json(snapshot.model_dump_json())
        self.inputs_json = encode(self.snapshot.model_dump(mode="json"))

    def reserve(self) -> bool:
        """Prevent two decisions sharing a calculator/audit trail."""
        if self._reserved or self.records:
            return False
        self._reserved = True
        return True

    async def monty_inputs(self) -> ToolResult[str]:
        """Inspect fixed calculation JSON: context, prices, account and portfolio."""
        return ToolResult(data=self.inputs_json)

    async def monty_calculate(self, code: str) -> ToolResult[CalculationRecord]:
        """Calculate from `inputs`; the last expression must serialize as JSON.

        Example: sum(float(p['price']) for p in inputs['prices'][0]['observations']).
        Use monty_inputs to inspect data. No host access, data fetch, clock or trades.
        """
        process: asyncio.subprocess.Process | None = None
        creation: asyncio.Task[asyncio.subprocess.Process] | None = None
        communication: asyncio.Task[tuple[bytes, bytes]] | None = None
        cancelled = False
        started = time.monotonic()
        status: Status = "worker_error"
        output: str | None = None
        error_text: str | None = None
        prints_json = "[]"
        with logfire.span("Monty calculation", _span_name="monty.calculate") as span:
            try:
                payload = encode({"code": code, "inputs": json.loads(self.inputs_json)}).encode()
                span.set_attribute("code_digest", digest(code))
                span.set_attribute("snapshot_digest", digest(self.inputs_json))
                # Isolated mode ignores Python-path/user-site overrides. Credentials,
                # home, gateway configuration and other environment values are absent.
                creation = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-m",
                        "bazaar_agent.monty_worker",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={"PYTHONUTF8": "1"},
                    )
                )
                process = await asyncio.shield(creation)
                communication = asyncio.create_task(process.communicate(payload))
                raw, stderr = await asyncio.shield(communication)
                if process.returncode == 0:
                    result = json.loads(raw)
                    # IPC failures are fatal boundaries, not correctable model math errors.
                    if not isinstance(result, dict) or result.get("status") not in (
                        "ok",
                        "denied",
                        "syntax",
                        "runtime",
                        "serialization",
                        "worker_error",
                    ):
                        raise ValueError("Invalid calculation worker response")
                    output = result.get("output_json")
                    error_text = result.get("error_text")
                    prints = result.get("prints", [])
                    if (
                        not isinstance(prints, list)
                        or (error_text is not None and not isinstance(error_text, str))
                        or (output is not None and not isinstance(output, str))
                        or (result["status"] == "ok" and output is None)
                        or (result["status"] != "ok" and output is not None)
                    ):
                        raise ValueError("Invalid calculation worker payload")
                    if output is not None:
                        json.loads(output)
                    prints_json = encode(prints)
                    status = result["status"]
                else:
                    error_text = f"Worker exited with status {process.returncode}: {stderr.decode(errors='replace')}"
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:  # noqa: BLE001 -- private record, never span exception text
                status = "worker_error"
                output = None
                error_text = str(exc)
            finally:
                cleanup = asyncio.create_task(_reap(creation, process, communication))
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
            span.set_attribute("status", "cancelled" if cancelled else status)
            span.set_attribute("duration_seconds", time.monotonic() - started)
        record = CalculationRecord(
            execution_id=uuid4(),
            context=self.snapshot.context,
            code=code,
            code_digest=digest(code),
            inputs_json=self.inputs_json,
            snapshot_digest=digest(self.inputs_json),
            status="cancelled" if cancelled else status,
            output_json=output,
            error_text=error_text,
            prints_json=prints_json,
            duration_seconds=time.monotonic() - started,
        )
        self.records.append(record)
        if cancelled:
            raise asyncio.CancelledError
        if status == "ok":
            return ToolResult(data=record)
        # Detailed SDK diagnostics belong to the caller-owned audit record, not telemetry.
        return ToolResult(
            data=record,
            error=ToolError(
                code="unsupported" if status == "denied" else "invalid_request",
                message=f"Monty calculation failed: {status}",
            ),
        )

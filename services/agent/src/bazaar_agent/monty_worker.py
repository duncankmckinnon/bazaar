"""Private Monty worker: JSON IPC, no host-call continuation or inherited credentials."""

import json
import sys

import pydantic_monty as monty


def calculate(request: dict) -> dict:
    prints: list[tuple] = []
    try:
        runner = monty.Monty(request["code"], inputs=["inputs"], script_name="calculation.py")
        result = runner.start(
            inputs={"inputs": request["inputs"]},
            print_callback=lambda *parts: prints.append(parts),
        )
        # OS/external/name-lookup snapshots are never resumed. No mount, OS handler,
        # host callback or credential is supplied. Pure SDK-supported math is available.
        if not isinstance(result, monty.MontyComplete):
            return {
                "status": "denied",
                "error_text": "Host access is unavailable",
                "prints": prints,
            }
        return {
            "status": "ok",
            "output_json": json.dumps(result.output, allow_nan=False),
            "prints": prints,
        }
    except monty.MontySyntaxError as exc:
        return {"status": "syntax", "error_text": str(exc), "prints": prints}
    except monty.MontyRuntimeError as exc:
        return {"status": "runtime", "error_text": str(exc), "prints": prints}
    except Exception as exc:  # noqa: BLE001 -- private audit response, never ordinary telemetry
        return {"status": "serialization", "error_text": str(exc), "prints": prints}


def main() -> None:
    try:
        response = calculate(json.load(sys.stdin))
    except Exception as exc:  # noqa: BLE001
        response = {"status": "worker_error", "error_text": str(exc), "prints": []}
    sys.stdout.write(json.dumps(response))


if __name__ == "__main__":
    main()

"""Exercise the built runner and online judge against the real Docker market.

Only models are replaced with deterministic local fixtures. No paid model or
Logfire calls: invoke in the isolated smoke project with BAZAAR_DOCKER_SMOKE=1.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import logfire
from bazaar_agent import strategy_evaluation
from bazaar_runner import submission
from bazaar_runner.agent import fixture_model_factory
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

assert os.environ.get("BAZAAR_DOCKER_SMOKE") == "1", "Use only in the isolated smoke project"
assert not os.environ.get("PYDANTIC_AI_GATEWAY_API_KEY")
assert not os.environ.get("LOGFIRE_TOKEN")
logfire.configure(send_to_logfire=False, console=False)
submission.configure_telemetry = lambda: None
submission._model_factory = lambda model: fixture_model_factory()
judge_calls = []


def grade(messages, info):
    judge_calls.append(messages)
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name,
                {"pass": True, "score": 1.0, "reason": "Deterministic Docker smoke-test grade."},
            )
        ]
    )


os.environ["BAZAAR_STRATEGY_EVAL_ENABLED"] = "1"
strategy_evaluation.judge_model = lambda: FunctionModel(grade)
days = []
run_dir = submission.run_submission(
    submission_id=f"docker-smoke-{uuid4()}",
    name="docker-smoke",
    instructions="Read the news once, buy ten AAPL at the first open, then hold.",
    market_url=os.environ["BAZAAR_MARKET_URL"],
    runner_token=os.environ["BAZAAR_RUNNER_TOKEN"],
    runs_dir=Path(os.environ["BAZAAR_RUNS_DIR"]),
    on_progress=days.append,
)
record = json.loads((run_dir / "record.json").read_text())
assert record["status"] == "completed", record["status"]
assert days == list(range(1, 11)), days
assert record["orders"][0]["result"]["status"] == "filled"
assert (run_dir / "evaluation.json").is_file()
assert len(judge_calls) == 10, len(judge_calls)
print("Docker run completed: 10 sessions, filled order, outcome score, 10 online judge calls.")

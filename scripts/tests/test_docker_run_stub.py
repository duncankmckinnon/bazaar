"""The Docker smoke script's judge stub matches how the production judge calls judge_model."""

import ast
from pathlib import Path

import httpx2

DOCKER_RUN = Path(__file__).with_name("docker_run.py")


def stub_source() -> str:
    tree = ast.parse(DOCKER_RUN.read_text())
    (stub,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(ast.unparse(t) == "strategy_evaluation.judge_model" for t in node.targets)
    ]
    return ast.unparse(stub)


def test_the_judge_stub_takes_the_judges_http_client():
    calls = []

    def FunctionModel(grade):
        calls.append(grade)
        return "model"

    stub = eval(stub_source(), {"FunctionModel": FunctionModel, "grade": "grade"})
    client = httpx2.AsyncClient()
    # _judge calls judge_model(client) (positionally), and a required argument must also work.
    assert stub(client) == "model" and stub(http_client=client) == "model"
    assert calls == ["grade", "grade"]

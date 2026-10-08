from bazaar_market.app import create_app
from fastapi.testclient import TestClient


def test_health(tmp_path):
    with TestClient(create_app(tmp_path / "market.sqlite3")) as client:
        assert client.get("/health").json() == {"status": "ok"}

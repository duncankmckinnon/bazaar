from bazaar_market.app import app
from fastapi.testclient import TestClient


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}

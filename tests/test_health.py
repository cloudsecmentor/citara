import pytest
from fastapi.testclient import TestClient


def test_health_endpoint_returns_ok():
    from hermes_knowledge.adapters.api.main import create_app

    app = create_app()
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

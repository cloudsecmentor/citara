from fastapi.testclient import TestClient


def test_health_endpoint_returns_ok():
    from citara.adapters.api.main import create_app

    app = create_app()
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

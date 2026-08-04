from fastapi.testclient import TestClient


def test_health_endpoint_returns_ok():
    from citara import __version__
    from citara.adapters.api.main import create_app

    app = create_app()
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    # Additive: `version` was added alongside the original `status` key so
    # existing consumers checking `status` keep working unmodified.
    assert response.json() == {"status": "ok", "version": __version__}

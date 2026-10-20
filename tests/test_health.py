from fastapi.testclient import TestClient

from evaluation_backend.api import create_app


def test_health_is_process_liveness_only():
    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "service": "evaluation-backend"}

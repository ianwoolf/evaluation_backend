import pytest
from fastapi.testclient import TestClient
from evaluation_backend.api import create_app
from evaluation_backend.storage.service import get_service, EvaluationStorageService
from tests.memory_storage import Memory
from evaluation_backend.runs import kube


@pytest.fixture
def standalone(monkeypatch):
    monkeypatch.setenv('EVAL_AUTH_MODE', 'none')
    monkeypatch.setenv('EVAL_DEFAULT_PROJECT_ID', 'individual')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'internal-signing-only')
    monkeypatch.delenv('KUBERNETES_SERVICE_HOST', raising=False)
    monkeypatch.delenv('EVAL_COLLECTOR_LOCAL', raising=False)
    app = create_app()
    storage = Memory()
    storage.put('datasets', 'individual', 'dataset', {'id':'dataset', 'draft':{}, 'versions':[]})
    app.dependency_overrides[get_service] = lambda: EvaluationStorageService(storage)
    with TestClient(app) as client:
        yield client


def test_public_evaluation_api_has_no_login_or_project_service(standalone):
    response = standalone.get('/api/v1/evaluations/datasets')
    assert response.status_code == 200
    assert response.json()['items'][0]['id'] == 'dataset'
    assert standalone.get('/api/v1/evaluations/datasets', headers={'X-Project-ID':'other'}).status_code == 400
    assert standalone.get('/api/v1/evaluations/datasets', headers={'Authorization':'Bearer wrong'}).status_code == 401
    assert standalone.get('/internal/models/private').status_code == 401
    assert standalone.post('/api/v1/evaluations/runs', content=b' ' * 40001, headers={'Content-Type':'application/json'}).status_code == 413


def test_single_user_mode_preserves_worker_capability_boundary(standalone):
    from evaluation_backend.run_auth import issue_worker_token
    token = issue_worker_token('individual', 'r')
    assert standalone.get('/api/v1/evaluations/datasets', headers={'Authorization':'Bearer '+token}).status_code == 403


def test_token_mode_remains_available(monkeypatch):
    monkeypatch.setenv('EVAL_AUTH_MODE', 'token')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'internal')
    monkeypatch.delenv('KUBERNETES_SERVICE_HOST', raising=False)
    with TestClient(create_app()) as client:
        assert client.get('/api/v1/evaluations/datasets', headers={'X-Project-ID':'individual'}).status_code == 401


def test_explicit_cors_origin(monkeypatch):
    monkeypatch.setenv('EVAL_AUTH_MODE', 'none')
    monkeypatch.setenv('EVAL_UI_ORIGINS', 'http://localhost:8080')
    monkeypatch.delenv('KUBERNETES_SERVICE_HOST', raising=False)
    with TestClient(create_app()) as client:
        response = client.options('/api/v1/evaluations/datasets', headers={
            'Origin':'http://localhost:8080','Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'Content-Type,Idempotency-Key,X-Project-ID'})
        assert response.status_code == 200
        assert response.headers['access-control-allow-origin'] == 'http://localhost:8080'

import pytest
from fastapi.testclient import TestClient
from evaluation_backend.collector import create_collector
from evaluation_backend.storage.service import get_service


def test_langsmith_is_explicitly_unimplemented(monkeypatch):
    monkeypatch.setenv('EVAL_COLLECTOR_EXPORTER', 'langsmith')
    with pytest.raises(NotImplementedError):
        create_collector()


def test_otlp_json_exports_trace_with_runner_identity(monkeypatch):
    monkeypatch.setenv('EVAL_COLLECTOR_EXPORTER', 'elasticsearch')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'test-token')
    monkeypatch.setenv('EVAL_PROJECT_ID', 'project')
    monkeypatch.setenv('EXPERIMENT_ID', 'exp-1')
    saved = []
    class Store:
        def put(self, *args):
            saved.append(args)
    class Service:
        storage = Store()
    app = create_collector()
    app.dependency_overrides[get_service] = lambda: Service()
    with TestClient(app) as client:
        payload = {'resourceSpans': [{'scopeSpans': [{'spans': [{'traceId': 'a', 'spanId': 'b', 'name': 'test'}]}]}]}
        assert client.post('/v1/traces', json=payload).status_code == 401
        assert client.post('/v1/traces', json=payload, headers={'Authorization': 'Bearer test-token'}).status_code == 200
    assert saved[0][:3] == ('traces', 'project', 'exp-1:a:b')

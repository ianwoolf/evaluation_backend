"""Opt-in real ES integration: EVAL_TEST_ES_URL=http://127.0.0.1:19200 pytest ..."""
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from evaluation_backend.storage.elastic import ElasticsearchStorage
from evaluation_backend.storage.service import EvaluationStorageService, DatasetDocument, get_service
from evaluation_backend.collector import create_collector
from evaluation_backend.api import create_app


@pytest.mark.skipif(not os.getenv('EVAL_TEST_ES_URL'), reason='Real ES endpoint not selected')
def test_collector_and_report_with_real_es(monkeypatch):
    project, evaluation = 'integration-test', 'exp-' + uuid.uuid4().hex
    monkeypatch.setenv('EVAL_PROJECT_ID', project)
    monkeypatch.setenv('EXPERIMENT_ID', evaluation)
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'integration-local')
    store = ElasticsearchStorage(os.environ['EVAL_TEST_ES_URL'])
    store.initialize()
    service = EvaluationStorageService(store)
    headers = {'Authorization': 'Bearer integration-local', 'X-Project-ID': project}
    collector, api = create_collector(), create_app()
    collector.dependency_overrides[get_service] = lambda: service
    api.dependency_overrides[get_service] = lambda: service
    with TestClient(collector) as sink, TestClient(api) as backend:
        summary = {'status': 'RUNNING', 'dataset_id': 'integration', 'dataset_version': 1, 'items': 0, 'errors': 0}
        base = '/api/v1/evaluations/'
        env_path = base + 'environments/' + evaluation
        config = {'name':'Integration','modelId':'glm-dev','guardrailId':'g','cpuMillicores':750,'memoryMiB':768}
        saved = backend.post(env_path,json={'config':config},headers=headers)
        assert saved.status_code == 200, saved.text
        updated = backend.patch(env_path,json={'revision':saved.json()['revision'],'cpuMillicores':500},headers=headers)
        assert updated.status_code == 200, updated.text
        assert backend.get(env_path,headers=headers).json()['versions'][-1]['cpuMillicores'] == 500
        assert backend.get(env_path+'/versions/1',headers=headers).json()['cpuMillicores'] == 750
        assert backend.patch(env_path,json={'revision':saved.json()['revision'],'cpuMillicores':600},headers=headers).status_code == 409
        assert sink.post(base + 'experiments/' + evaluation, json=summary, headers=headers).status_code == 200
        sample = {'id': 'sample-1', 'input': {'messages': []}, 'reference': {'has_issue': False},
                  'output': {'has_issue': False}, 'status': 'PASS', 'latencyMs': 1, 'scores': [
                      {'scorerId': 'mock-score', 'value': 1, 'passed': True, 'reason': 'MOCK integration test'}], 'trace': []}
        for _ in range(2):
            response = sink.post(base + 'experiments/' + evaluation + '/samples', json=sample, headers=headers)
            assert response.status_code == 200, response.text
        otlp = {'resourceSpans': [{'scopeSpans': [{'spans': [{'traceId': 'a' * 32, 'spanId': 'b' * 16, 'name': 'integration.mock'}]}]}]}
        assert sink.post('/v1/traces', json=otlp, headers=headers).status_code == 200
        summary.update(status='COMPLETED', items=1)
        assert sink.post(base + 'experiments/' + evaluation, json=summary, headers=headers).status_code == 200
        report = backend.get(base + 'reports/' + evaluation, headers=headers)
        assert report.status_code == 200, report.text
        assert report.json()['total'] == 1 and report.json()['results'][0]['scores'][0]['value'] == 1
        assert backend.get(base + 'reports/' + evaluation, headers={**headers, 'X-Project-ID': 'other'}).status_code == 404
        assert backend.get(base + 'scores', params={'evaluation_id': evaluation}, headers=headers).json()['total'] == 1
        assert backend.get(base + 'traces', params={'evaluation_id': evaluation}, headers=headers).json()['total'] == 2
    print('Verified real ES evaluation:', evaluation)

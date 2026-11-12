import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from evaluation_backend.api import create_app
from evaluation_backend.runs import PREFIX, kube, project_label
from evaluation_backend.storage import StorageError
from evaluation_backend.storage.elastic import ElasticsearchStorage
from evaluation_backend.storage.service import EvaluationStorageService, get_service
from memory_storage import Memory

ROOT = '/api/v1/evaluations/admin'
RUN = 'exp-' + 'a' * 32
JOB = 'eval-' + 'a' * 24


def test_admin_requires_token_and_keeps_job_and_report_deletion_separate(monkeypatch):
    monkeypatch.setenv('EVAL_AUTH_MODE', 'none')
    monkeypatch.setenv('EVAL_ADMIN_TOKEN', 'admin-test')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'trigger-test')
    monkeypatch.setenv('EVAL_DEFAULT_PROJECT_ID', 'p')
    memory = Memory()
    record = {'id': RUN, 'job_name': JOB, 'status': 'COMPLETED'}
    memory.put('experiments', 'p', RUN, record)
    memory.delete_run = lambda project, ident: (memory.data.pop(('experiments', project, ident)) and {'samples': 1, 'traces': 2})

    class Cluster:
        namespace = 'evaluation'
        jobs = '/jobs'
        job = {'metadata': {'name': JOB, 'annotations': {PREFIX + 'project': 'p'},
                            'labels': {'experiment_id': RUN}, 'uid': 'uid', 'resourceVersion': '3'}}

        pod = {'metadata': {'name': JOB + '-pod', 'uid': 'pod-uid', 'resourceVersion': '4',
                            'labels': {'app': 'evaluation-worker', 'project': project_label('p')},
                            'ownerReferences': [{'kind': 'Job', 'controller': True, 'name': JOB, 'uid': 'uid'}]},
               'status': {'phase': 'Succeeded'}}

        def call(self, method, path, **kwargs):
            if path == self.jobs:
                return {'items': [self.job] if self.job else []}
            if path.endswith('/pods'):
                return {'items': [self.pod] if self.pod else []}
            if '/pods/' in path:
                if method == 'DELETE':
                    assert kwargs['json']['preconditions']['uid'] == 'pod-uid'
                    self.pod = None
                return self.pod
            if not self.job:
                raise HTTPException(404)
            if method == 'DELETE':
                assert kwargs['json'] == {'propagationPolicy': 'Foreground',
                    'preconditions': {'uid': 'uid', 'resourceVersion': '3'}}
                self.job = None
            return self.job

    cluster = Cluster()
    app = create_app()
    app.dependency_overrides[get_service] = lambda: EvaluationStorageService(memory)
    app.dependency_overrides[kube] = lambda: cluster
    client = TestClient(app)
    headers = {'Authorization': 'Bearer admin-test'}
    assert client.get(ROOT + '/cleanup').status_code == 401
    assert client.delete(ROOT + '/jobs/' + JOB, headers={'Authorization': 'Bearer trigger-test'}).status_code == 401
    assert client.delete(ROOT + '/jobs/' + JOB, headers=headers).status_code == 409
    assert client.delete(ROOT + '/reports/' + RUN, headers=headers).status_code == 409
    pod_endpoint = ROOT + '/pods/' + JOB + '-pod'
    assert client.delete(pod_endpoint, headers=headers).status_code == 409
    cluster.job['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
    assert client.get(ROOT + '/cleanup', headers=headers).json()['reports'][0]['job_present']
    cluster.pod['metadata']['labels']['project'] = project_label('other')
    assert client.delete(pod_endpoint, headers=headers).status_code == 404
    cluster.pod['metadata']['labels']['project'] = project_label('p')
    assert client.get(ROOT + '/cleanup', headers=headers).json()['pods'][0]['deletable']
    assert client.delete(pod_endpoint, headers=headers).status_code == 200
    assert cluster.job is not None
    assert client.delete(ROOT + '/jobs/' + JOB, headers=headers).status_code == 200
    assert memory.get('experiments', 'p', RUN)['status'] == 'COMPLETED'
    assert client.delete(ROOT + '/reports/' + RUN, headers=headers).json()['traces'] == 2


def test_es_cleanup_scopes_evidence_and_stops_before_removing_record_on_failure():
    import json
    requests = []
    fail_trace = True

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'deleted': 1, 'failures': ['error'] if fail_trace and 'traces' in request.url.path else []})

    storage = ElasticsearchStorage('http://es:9200', client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(StorageError):
        storage.delete_run('p', RUN)
    assert len(requests) == 2
    assert all(request.method == 'POST' for request in requests)
    fail_trace = False
    assert storage.delete_run('p', RUN) == {'samples': 1, 'traces': 1}
    for request, field in zip(requests[-3:-1], ('run_id', 'evaluation_id')):
        assert json.loads(request.content)['query']['bool']['filter'] == [
            {'term': {'project_id': 'p'}}, {'term': {field: RUN}},
        ]
    assert requests[-1].method == 'DELETE'
    assert requests[-1].url.path.endswith(storage.record_key('experiments', 'p', RUN))


def test_report_inventory_filters_all_pages_and_sorts_by_creation_time():
    from evaluation_backend.cleanup_admin import inventory

    class Cluster:
        namespace = 'evaluation'
        jobs = '/jobs'

        def call(self, method, path, **kwargs):
            return {'items': []}

    store = Memory()
    for index in range(105):
        ident = f'exp-{index:032x}'
        store.put('experiments', 'p', ident, {
            'id': ident, 'name': 'Regression smoke', 'status': 'COMPLETED',
            'created_at': f'2026-11-{1 + index // 24:02d}T{index % 24:02d}:00:00Z',
        })
    store.put('experiments', 'p', RUN, {
        'id': RUN, 'name': 'Regression Smoke (target)', 'status': 'FAILED',
        'created_at': '2026-11-10T00:00:00+08:00',
    })
    common = {'x_project_id': 'p', 'cluster': Cluster(), 'service': EvaluationStorageService(store)}
    newest = inventory(offset=0, q='REGRESS smoke', **common)
    assert newest['total'] == 106
    assert newest['reports'][0]['id'] == RUN
    assert len(newest['reports']) == 100
    oldest = inventory(offset=100, sort='oldest', q='regress smoke', **common)
    assert len(oldest['reports']) == 6
    assert oldest['reports'][-1]['id'] == RUN
    filtered = inventory(offset=0, status='FAILED', q='oke TARGET', **common)
    assert filtered['total'] == 1
    assert filtered['reports'][0]['id'] == RUN

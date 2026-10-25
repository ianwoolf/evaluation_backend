import copy
import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from evaluation_backend.api import create_app
from evaluation_backend.runs import kube, PREFIX
from evaluation_backend.storage.service import get_service, EvaluationStorageService
from memory_storage import Memory
from test_uploads import SCHEMA


class Cluster:
    namespace = 'evaluation'
    jobs = '/jobs'
    def __init__(self):
        self.saved = {}
        self.created = 0
    def call(self, method, path, **kwargs):
        if method == 'POST':
            job = copy.deepcopy(kwargs['json'])
            job['metadata']['creationTimestamp'] = '2026-10-20T00:00:00Z'
            self.saved[job['metadata']['name']] = job
            self.created += 1
            return job
        if path == '/jobs':
            return {'items': list(self.saved.values())}
        if path.endswith('/pods'):
            return {'items': [{'metadata': {'name': 'worker-pod'}}]}
        if path.endswith('/log'):
            job = next(iter(self.saved.values()))
            return 'private unstructured log\n' + json.dumps({'evaluation_id': job['metadata']['labels']['experiment_id'], 'items': 6, 'url': 'http://internal:3000/project/p/datasets/d/runs/r'})
        if path.split('/')[-1] not in self.saved:
            raise HTTPException(404)
        return self.saved[path.split('/')[-1]]


@pytest.fixture
def setup(monkeypatch):
    binding = {'target_id': 'g', 'dataset_id': 'd', 'dataset_version': 1, 'environment_id': 'e', 'model_id': 'glm-dev', 'langfuse_dataset_id': 'real-d', 'langfuse_dataset_version': '2026-10-19T12:00:00Z'}
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'secret')
    monkeypatch.setenv('EVAL_TARGETS', json.dumps([{'id':'g','name':'Guardrail','provider':'nemo guardrail','version':2}]))
    monkeypatch.setenv('EVAL_RESOURCE_BINDINGS', json.dumps([binding]))
    monkeypatch.setenv('EVAL_WORKER_IMAGE', 'ianwoolf/evaluation-worker:test')
    monkeypatch.setenv('EVAL_PUBLIC_LANGFUSE_URL', 'http://localhost:3000')
    cluster = Cluster()
    app = create_app()
    memory = Memory()
    memory.put("datasets", "p", "d", {"versions": [SCHEMA]})
    memory.put('environments','p','e',{'id':'e','versions':[{'name':'Isolated','version':1,'cpuMillicores':750,'memoryMiB':768,'modelId':'glm-dev','guardrailId':'g'}]})
    app.dependency_overrides[get_service] = lambda: EvaluationStorageService(memory)
    app.dependency_overrides[kube] = lambda: cluster
    body = {'name': 'Test', **{k: v for k, v in binding.items() if not k.startswith('langfuse')}, 'scoring_mode': 'mock', 'scorer_ids': ['mock-score'], 'snapshot': {}}
    headers = {'Authorization': 'Bearer secret', 'X-Project-ID': 'p', 'Idempotency-Key': 'test-1234567890123456'}
    return TestClient(app), cluster, body, headers


def test_auth_and_binding_validation(setup):
    client, cluster, body, headers = setup
    assert client.post('/api/v1/evaluations/runs', json=body).status_code == 401
    body['model_id'] = 'arbitrary'
    assert client.post('/api/v1/evaluations/runs', json=body, headers=headers).status_code == 422
    assert not cluster.created


def test_launch_idempotency_pinning_and_project_isolation(setup):
    client, cluster, body, headers = setup
    first = client.post('/api/v1/evaluations/runs', json=body, headers=headers)
    assert first.status_code == 200, first.text
    run = first.json()
    assert run['status'] == 'RUNNING'
    assert run['experiment_id'].startswith('exp-')
    assert client.post('/api/v1/evaluations/runs', json=body, headers=headers).json()['id'] == run['id']
    assert cluster.created == 1
    job = cluster.saved[run['id']]
    env = {e['name']: e['value'] for e in job['spec']['template']['spec']['containers'][0]['env']}
    assert env['DATASET_ID'] == 'd'
    assert env['EVAL_DATASET_VERSION'] == '1'
    assert env['EVAL_RUN_INPUT_ID'] == run['experiment_id']
    assert env['EXPERIMENT_ID'] == env['EVALUATE_ID'] == run['experiment_id']
    assert job['spec']['template']['spec']['automountServiceAccountToken'] is False
    assert client.get('/api/v1/evaluations/runs/' + run['id'], headers={**headers, 'X-Project-ID': 'other'}).status_code == 404
    body['name'] = 'Changed'
    assert client.post('/api/v1/evaluations/runs', json=body, headers=headers).status_code == 409


def test_completion_only_returns_structured_worker_summary(setup):
    client, cluster, body, headers = setup
    run = client.post('/api/v1/evaluations/runs', json=body, headers=headers).json()
    cluster.saved[run['id']]['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
    result = client.get('/api/v1/evaluations/runs/' + run['id'], headers=headers).json()
    assert result['status'] == 'COMPLETED'
    assert result['completed'] == 0  # No persisted samples; do not trust stdout sample counts.
    assert 'report_url' not in result
    assert 'private' not in json.dumps(result)


def test_installed_scorers_not_silently_mocked(setup):
    client, cluster, body, headers = setup
    body['scoring_mode'] = 'installed'
    assert client.post('/api/v1/evaluations/runs', json=body, headers=headers).status_code == 422
    assert not cluster.created


def test_environment_resources_reach_pod_and_snapshot(setup):
    client, cluster, body, headers = setup
    body['resources'] = {'cpu_millicores': 750, 'memory_mib': 768}
    body['environment_version'] = 1
    response = client.post('/api/v1/evaluations/runs', json=body, headers=headers)
    assert response.status_code == 200, response.text
    run = response.json()
    resources = cluster.saved[run['id']]['spec']['template']['spec']['containers'][0]['resources']
    assert resources == {'requests': {'cpu': '750m', 'memory': '768Mi'}, 'limits': {'cpu': '750m', 'memory': '768Mi'}}
    assert run['snapshot']['environment']['version'] == 1
    assert run['snapshot']['environment']['cpuMillicores'] == 750
    assert run['snapshot']['environment']['memoryMiB'] == 768
    body['resources']['cpu_millicores'] = 1000
    assert client.post('/api/v1/evaluations/runs', json=body, headers=headers).status_code == 409


@pytest.mark.parametrize('resources', [
    {'cpu_millicores': 0, 'memory_mib': 128},
    {'cpu_millicores': 32001, 'memory_mib': 128},
    {'cpu_millicores': 0.5, 'memory_mib': 128},
    {'cpu_millicores': '500m', 'memory_mib': 128},
    {'cpu_millicores': True, 'memory_mib': 128},
    {'cpu_millicores': 500, 'memory_mib': 127},
    {'cpu_millicores': 500, 'memory_mib': 65537},
    {'cpu_millicores': 500, 'memory_mib': 256, 'privileged': True},
])
def test_invalid_environment_resources_never_create_job(setup, resources):
    client, cluster, body, headers = setup
    body['resources'] = resources
    assert client.post('/api/v1/evaluations/runs', json=body, headers=headers).status_code == 422
    assert cluster.created == 0


def test_run_uses_database_environment_and_trusted_target(setup):
    c,k,b,h=setup
    b['snapshot']={'target':{'provider':'f5','name':'forged'}}
    response=c.post('/api/v1/evaluations/runs',json=b,headers=h)
    assert response.status_code==200,response.text
    run=response.json();job=k.saved[run['id']]
    assert job['spec']['template']['spec']['containers'][0]['resources']['limits']=={'cpu':'750m','memory':'768Mi'}
    env={v['name']:v['value'] for v in job['spec']['template']['spec']['containers'][0]['env']}
    target=json.loads(env['EVAL_TARGET_SNAPSHOT'])
    environment=json.loads(env['EVAL_ENVIRONMENT_SNAPSHOT'])
    assert environment['id']=='e' and environment['version']==1
    assert environment['guardrailId']=='g' and env['EVAL_GUARDRAIL_ID']=='g'
    assert environment['modelId']=='glm-dev'
    assert environment['cpuMillicores']==750 and environment['memoryMiB']==768
    assert target['provider']=='nemo guardrail' and target['name']=='Guardrail'
    assert run['snapshot']['target']['provider']=='nemo guardrail'


def test_caller_cannot_override_persisted_resources(setup):
    c,k,b,h=setup
    b['resources']={'cpu_millicores':1000,'memory_mib':768}
    assert c.post('/api/v1/evaluations/runs',json=b,headers=h).status_code==422
    assert k.created==0

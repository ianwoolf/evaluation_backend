import json
import pytest
from evaluation_backend.k8s_job import build_job


def test_fresh_identity_and_secret_references():
    a = build_job('dataset', 'glm-dev', ['mock-score'], mock=True)
    b = build_job('dataset', 'glm-dev', ['mock-score'], mock=True)
    ident = a['metadata']['labels']['experiment_id']
    assert ident != b['metadata']['labels']['experiment_id']
    pod = a['spec']['template']
    assert pod['metadata']['labels']['experiment_id'] == ident
    worker = pod['spec']['containers'][0]
    env = {x['name']: x['value'] for x in worker['env'] if 'value' in x}
    assert all(env[k] == ident for k in ('EXPERIMENT_ID', 'EVALUATE_ID', 'EVALUATION_ID'))
    assert env['EVAL_SCORER_MODE'] == 'mock'
    assert env['SCORER_IDS'] == '["mock-score"]'
    assert all('configMapRef' in item for item in worker['envFrom'])
    assert worker['envFrom'][1]['configMapRef']['name'] == 'evaluation-storage'
    refs = {x['name']:x['valueFrom']['secretKeyRef'] for x in worker['env'] if 'valueFrom' in x}
    assert refs['EVAL_MODEL_RESOLVER_TOKEN']['name'] == 'evaluation-credentials'
    assert 'EVAL_TRIGGER_TOKEN' not in refs
    assert 'LANGFUSE_SECRET_KEY' not in json.dumps(a)
    assert not pod['spec']['automountServiceAccountToken']
    assert a['spec']['backoffLimit'] == 0


def test_mock_and_namespace_validation():
    with pytest.raises(ValueError):
        build_job('dataset', 'glm-dev', ['real-scorer'], mock=True)
    with pytest.raises(ValueError):
        build_job('dataset', 'glm-dev', ['mock-score'], namespace='invalid/name')

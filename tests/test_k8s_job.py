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
    env = {x['name']: x['value'] for x in worker['env']}
    assert all(env[k] == ident for k in ('EXPERIMENT_ID', 'EVALUATE_ID', 'EVALUATION_ID'))
    assert env['EVAL_SCORER_MODE'] == 'mock'
    assert env['SCORER_IDS'] == '["mock-score"]'
    assert worker['envFrom'][1]['secretRef']['name'] == 'evaluation-runner'
    assert 'LANGFUSE_SECRET_KEY' not in json.dumps(a)
    assert not pod['spec']['automountServiceAccountToken']
    assert a['spec']['backoffLimit'] == 0


def test_mock_and_namespace_validation():
    with pytest.raises(ValueError):
        build_job('dataset', 'glm-dev', ['real-scorer'], mock=True)
    with pytest.raises(ValueError):
        build_job('dataset', 'glm-dev', ['mock-score'], namespace='invalid/name')

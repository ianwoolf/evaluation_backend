import inspect
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evaluation_backend.experiment import Settings, Score, run_experiment
from evaluation_backend.platforms.langfuse import LangfusePlatform
from evaluation_backend.scorers import InstalledScorerLibrary
from evaluation_backend.openshell_runner import OpenShellRunner
from evaluation_backend.worker import HttpGuardrail


def env():
    return {'DATASET_ID': 'dataset-id', 'EVALUATE_ID': 'evaluation-1',
            'SCORER_IDS': '["sc-risk"]', 'DATASET_VERSION': '2026-10-19T00:00:00Z'}


class FakeDataset:
    id, name = 'dataset-id', 'Guardrail conversation safety'
    metadata = {'evaluation': {'scorers': {'sc-risk': {'version': '1', 'library': 'risk-classification', 'threshold': .8}}}}
    items = [SimpleNamespace(input={'messages': [{'role': 'user', 'content': 'hi'}]}, expected_output={'has_issue': False})]

    def run_experiment(self, **kwargs):
        self.kwargs = kwargs
        results = []
        for item in self.items:
            evaluations = []
            try:
                output = kwargs['task'](item=item)
                for evaluator in kwargs['evaluators']:
                    try:
                        evaluations.append(evaluator(input=item.input, output=output, expected_output=item.expected_output))
                    except ValueError:
                        pass
            except ValueError:
                pass
            results.append(SimpleNamespace(evaluations=evaluations))
        return SimpleNamespace(item_results=results, dataset_run_url='https://example.test/experiment/1', experiment_id='native-id')


class FakeClient:
    def __init__(self):
        self.dataset, self.flushed = FakeDataset(), False

    def get_dataset(self, name, **kwargs):
        self.name, self.version = name, kwargs['version']
        return self.dataset

    def flush(self):
        self.flushed = True


def test_langfuse_reads_by_id_runs_and_reports_scores():
    settings = Settings.from_env(env())
    client = FakeClient()
    platform = LangfusePlatform(client, resolve_name=lambda ident: FakeDataset.name)
    inputs = []
    def target(value):
        inputs.append(value)
        return {'has_issue': False}
    result = run_experiment(settings, platform, InstalledScorerLibrary(), platform, target)
    assert result['status'] == 'COMPLETED'
    assert client.name == FakeDataset.name
    assert client.version == settings.dataset_version
    assert client.dataset.kwargs['name'] == settings.evaluation_id
    assert client.dataset.kwargs['run_name'] == settings.evaluation_id
    assert 'expected_output' not in inputs[0] and 'has_issue' not in inputs[0]
    assert client.flushed
    feedback = client.dataset.kwargs['evaluators'][0](input={}, output={'has_issue': True}, expected_output={'has_issue': False})
    assert feedback['name'] == 'sc-risk' and feedback['value'] == 0
    assert feedback['metadata']['experiment_id'] == settings.evaluation_id
    assert client.dataset.kwargs['metadata']['experiment_id'] == settings.evaluation_id
    assert 'False positive' in feedback['comment'] and 'scorerVersion=1' in feedback['comment']


@pytest.mark.parametrize('failure', ['target', 'scorer'])
def test_item_errors_cannot_become_success(failure):
    client = FakeClient()
    platform = LangfusePlatform(client, resolve_name=lambda ident: FakeDataset.name)
    def target(value):
        if failure == 'target':
            raise RuntimeError('private credentials must not leak')
        return {'has_issue': 'invalid boolean'}
    result = run_experiment(Settings.from_env(env()), platform, InstalledScorerLibrary(), platform, target)
    assert result['status'] == 'FAILED' and result['errors'] > 0
    assert client.flushed


@pytest.mark.parametrize('updates', [{'SCORER_IDS': '[]'}, {'SCORER_IDS': '["a","a"]'}, {'SCORER_IDS': '"a"'},
    {'DATASET_ID': ''}, {'EVALUATE_ID': ''}, {'EVAL_CONCURRENCY': '33'}, {'DATASET_VERSION': '2026-10-19'}])
def test_invalid_environment(updates):
    with pytest.raises(ValueError):
        Settings.from_env({**env(), **updates})


def test_unknown_scorer_rejected_before_target():
    platform = LangfusePlatform(FakeClient(), resolve_name=lambda ident: FakeDataset.name)
    with pytest.raises(ValueError, match='scorer metadata'):
        run_experiment(replace(Settings.from_env(env()), scorer_ids=['unknown']), platform, InstalledScorerLibrary(), platform, lambda _: pytest.fail('target executed'))


def test_score_validation_and_risk_false_negative():
    with pytest.raises(ValueError):
        Score(float('nan'), '')
    from evaluation_backend.scorers import risk_classification
    assert risk_classification(input={}, output={'has_issue': False}, reference={'has_issue': True}).reason == 'False negative'
    with pytest.raises(ValueError):
        HttpGuardrail('file:///etc/passwd')


def test_target_only_receives_messages_and_overridden_model(monkeypatch):
    from io import BytesIO
    import evaluation_backend.worker as module
    captured = []
    def request(req, timeout):
        captured.append(json.loads(req.data))
        return BytesIO(b'{"has_issue":false}')
    monkeypatch.setattr(module, 'urlopen', request)
    target = HttpGuardrail('https://guardrail.example.test', 'selected-model')
    messages = [{'role': 'user', 'content': 'reset my password'}]
    assert target({'messages': messages, 'has_issue': True, 'model': 'wrong'}) == {'has_issue': False}
    assert captured == [{'messages': messages, 'model': 'selected-model'}]


@pytest.mark.parametrize('mode', ['docker', 'k8s'])
def test_openshell_injects_ids_without_secret_argv(tmp_path, mode):
    policy = tmp_path / 'policy.yaml'
    policy.write_text('policy')
    config = {**env(), 'EVAL_RUNNER_MODE': mode, 'EVAL_OPENSHELL_' + mode.upper() + '_GATEWAY': 'test-gateway',
              'EVAL_WORKER_IMAGE': 'worker@sha256:test', 'EVAL_OPENSHELL_POLICY': str(policy),
              'LANGFUSE_SECRET_KEY': 'private-key'}
    calls = []
    def call(args, **kwargs):
        calls.append(args)
        assert 'private-key' not in repr(args)
        if 'create' in args:
            assert len(args[args.index('--name') + 1]) <= 19
            assert '--upload' not in args
        if 'upload' in args:
            from pathlib import Path
            uploaded = Path(args[args.index('upload') + 2])
            values = json.loads(uploaded.read_text())
            assert values['LANGFUSE_SECRET_KEY'] == 'private-key'
            assert values['DATASET_ID'] == 'dataset-id'
            assert values['SCORER_IDS'] == '["sc-risk"]'
            assert values['EXPERIMENT_ID'].startswith('exp-')
            assert values['EXPERIMENT_ID'] == values['EVALUATE_ID'] == values['EVALUATION_ID']
        return SimpleNamespace(returncode=0, stdout='{"status":"COMPLETED"}\n')
    OpenShellRunner(config, call).run()
    assert [args[args.index('sandbox') + 1] for args in calls] == ['create', 'upload', 'exec', 'delete']
    assert all(args[:3] == ['openshell', '--gateway', 'test-gateway'] for args in calls)


def test_openshell_failure_stops_but_preserves(tmp_path):
    policy = tmp_path / 'policy.yaml'
    policy.write_text('policy')
    config = {**env(), 'EVAL_OPENSHELL_DOCKER_GATEWAY': 'gateway', 'EVAL_WORKER_IMAGE': 'worker', 'EVAL_OPENSHELL_POLICY': str(policy)}
    calls = []
    def call(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if 'exec' in args else 0, stdout='')
    with pytest.raises(RuntimeError):
        OpenShellRunner(config, call).run()
    assert any('stop' in args for args in calls)
    assert not any('delete' in args for args in calls)


def test_installed_sdk_contract():
    pytest.importorskip('langfuse')
    from langfuse._client.datasets import DatasetClient
    from langfuse import Langfuse
    from langfuse.experiment import ExperimentResult
    assert 'dataset_run_url' in inspect.signature(ExperimentResult).parameters
    assert 'experiment_id' in inspect.signature(ExperimentResult).parameters
    assert 'version' in inspect.signature(Langfuse.get_dataset).parameters
    params = inspect.signature(DatasetClient.run_experiment).parameters
    assert all(x in params for x in ['name', 'task', 'evaluators', 'max_concurrency', 'metadata'])

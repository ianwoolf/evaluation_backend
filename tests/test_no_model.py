from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from evaluation_backend.experiment import Settings, Score, Scorer
from evaluation_backend.storage.experiment import StoredExperiment


@pytest.mark.parametrize('model', ['none', 'glm-dev'])
def test_model_bypass_preserves_scorer_contract_and_timing(monkeypatch, model):
    monkeypatch.setenv('EVAL_MODEL_ID', model)
    client = Mock()
    client.version.return_value = {'name': 'test', 'metadata': {}, 'fields': [
        {'name': 'messages', 'role': 'input'}, {'name': 'has_issue', 'role': 'reference'}],
        'rows': [{'messages': [{'role': 'user', 'content': 'hello'}], 'has_issue': False}]}
    settings = Settings('ds', 'eval', ['score'], datetime.now(timezone.utc))
    runner = StoredExperiment(client)
    dataset = runner.get_dataset(settings)
    target = Mock(return_value={'has_issue': False})
    evaluate = Mock(return_value=Score(1, 'ok'))
    result = runner.execute(settings, dataset, target, [Scorer('score', 'v1', .5, evaluate)])
    assert result['status'] == 'COMPLETED'
    sample = client.sample.call_args.args[1]
    assert evaluate.call_args.kwargs['reference'] == {'has_issue': False}
    assert sample['latencyMs'] >= sample['modelLatencyMs'] >= 0
    if model == 'none':
        target.assert_not_called()
        assert evaluate.call_args.kwargs['output'] == evaluate.call_args.kwargs['input']
        assert sample['modelLatencyMs'] == 0
        assert [step['kind'] for step in sample['trace']] == ['SCORER']
        client.trace.assert_not_called()  # Sample ingestion creates its evaluation.sample trace.
    else:
        target.assert_called_once()
        assert evaluate.call_args.kwargs['output'] == {'has_issue': False}
        assert sample['trace'][0]['kind'] == 'GUARDRAIL'


def test_worker_none_never_constructs_model_target(monkeypatch, capsys):
    from evaluation_backend import worker, llm_target
    from evaluation_backend.platforms import factory
    monkeypatch.setattr('sys.argv', ['worker'])
    for key, value in {'EVAL_MODEL_ID': 'none', 'EVAL_TARGET_MODE': 'llm', 'EVAL_SCORER_MODE': 'mock',
                       'DATASET_ID': 'ds', 'SCORER_IDS': '["mock-score"]', 'EVALUATE_ID': 'eval'}.items():
        monkeypatch.setenv(key, value)
    import json
    environment={'id':'env-test','version':3,'modelId':'none','guardrailId':'guardrail-test',
                 'cpuMillicores':500,'memoryMiB':1024,'network':['example.test']}
    monkeypatch.setenv('EVAL_ENVIRONMENT_SNAPSHOT', json.dumps(environment))
    monkeypatch.setenv('EVAL_STORAGE_TOKEN', 'must-not-print-this-token')
    monkeypatch.delenv('EVAL_TARGET_SNAPSHOT', raising=False)
    monkeypatch.setattr(llm_target, 'LlmClassificationTarget', Mock(side_effect=AssertionError('model constructed')))
    monkeypatch.setattr(factory, 'create_platform', Mock())
    run = Mock(return_value={'status': 'COMPLETED'})
    monkeypatch.setattr(worker, 'run_experiment', run)
    assert worker.main() == 0
    assert run.call_args.args[-1] is None
    output=capsys.readouterr().out
    event=next(json.loads(line) for line in output.splitlines() if json.loads(line).get('event')=='evaluation.environment')
    assert event['environment']==environment
    assert 'must-not-print-this-token' not in output


def test_none_supervisor_keeps_collector_without_llm_proxy(monkeypatch):
    from evaluation_backend import proxy_entrypoint
    monkeypatch.setenv('EVAL_MODEL_ID', 'none')
    monkeypatch.setattr('sys.argv', ['proxy_entrypoint'])
    collector = Mock()
    collector.poll.return_value = None
    child = Mock()
    child.poll.return_value = 0
    child.returncode = 0
    spawn = Mock(side_effect=[collector, child])
    monkeypatch.setattr(proxy_entrypoint.subprocess, 'Popen', spawn)
    health = Mock()
    health.return_value.__enter__ = Mock()
    health.return_value.__exit__ = Mock()
    monkeypatch.setattr(proxy_entrypoint, 'urlopen', health)
    assert proxy_entrypoint.main() == 0
    assert spawn.call_count == 2
    assert spawn.call_args_list[0].args[0][-1] == 'evaluation_backend.collector'
    assert spawn.call_args_list[1].args[0][-1] == 'evaluation_backend.worker'
    assert spawn.call_args_list[1].kwargs['env']['EVAL_LLM_PROXY_ENABLED'] == 'false'
    assert health.call_count == 1
    assert ':18091/health' in health.call_args.args[0]


def test_model_latency_survives_content_redaction():
    from evaluation_backend.content_policy import ContentPolicy
    from evaluation_backend.storage.api import SampleDocument
    policy = ContentPolicy(input='omit', response='omit', reference='omit')
    sample = SampleDocument(id='s', input={}, reference={}, output={}, status='PASS',
                            latencyMs=12, modelLatencyMs=0, scores=[], trace=[])
    assert policy.sample(sample.model_dump())['modelLatencyMs'] == 0

from evaluation_backend.telemetry import prepare_experiment
from evaluation_backend.llm_target import MockScorerLibrary


def test_new_identity_each_trigger_and_preserve_in_child():
    env = {'EVALUATE_ID': 'dev_1', 'EVALUATION_ID': 'old'}
    first = prepare_experiment(env, fresh=True)
    assert first.startswith('exp-') and first != 'dev_1'
    assert all(env[k] == first for k in ('EXPERIMENT_ID', 'EVALUATE_ID', 'EVALUATION_ID'))
    assert prepare_experiment(env) == first
    assert prepare_experiment(env, fresh=True) != first


def test_mock_library_does_not_need_dataset_metadata():
    scorers = MockScorerLibrary().get_scorers(None, ['mock-score'])
    score = scorers[0].evaluate(input={}, output={}, reference={})
    assert score.value == 1 and 'MOCK' in score.reason

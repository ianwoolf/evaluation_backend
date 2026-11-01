from evaluation_backend.demo_scoring import DemoScorerLibrary
from evaluation_backend.experiment import Dataset

def test_demo_scores_preserve_ids_and_do_not_load_plugins():
    library = DemoScorerLibrary()
    scorers = library.get_scorers(Dataset('ds','Demo',{}), ['risk','helpfulness'])
    assert [s.id for s in scorers] == ['risk','helpfulness']
    for scorer in scorers:
        first = scorer.evaluate(input={'q':'test'}, output={}, reference={})
        assert first == scorer.evaluate(input={'q':'test'}, output={}, reference={})
        assert 0 <= first.value <= 1
        assert 'MOCK' in first.reason

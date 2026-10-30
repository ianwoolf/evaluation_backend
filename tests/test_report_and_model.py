from evaluation_backend.llm_target import LlmClassificationTarget
from evaluation_backend.storage.api import report


def test_target_model_port_does_not_receive_reference():
    class Client:
        def complete(self, messages, *, options=None):
            assert 'reference' not in str(messages)
            assert options['response_format'] == {'type': 'json_object'}
            return '{"has_issue": false}'
    assert LlmClassificationTarget(Client())({'messages': [{'role': 'user', 'content': 'hello'}]}) == {'has_issue': False}


def test_report_reads_project_scoped_partial_samples():
    class Store:
        def get(self, collection, project, ident):
            assert (collection, project, ident) == ('experiments', 'p', 'exp-1')
            return {'status': 'RUNNING'}
    class Service:
        storage = Store()
        def dataset_summary(self, project, ident, experiment):
            assert (project, ident, experiment['status']) == ('p', 'exp-1', 'RUNNING')
            return []
        def page(self, collection, project, **kwargs):
            assert collection == 'samples' and project == 'p'
            assert kwargs['filters'] == {'evaluation_id': 'exp-1'}
            return {'items': [{'id': 'sample-1'}], 'total': 2}
    value = report('exp-1', 'p', 0, 1, Service())
    assert value['partial'] and value['nextOffset'] == 1
    assert value['results'] == [{'id': 'sample-1'}]

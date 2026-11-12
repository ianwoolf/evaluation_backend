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
            if collection == 'run-contexts':
                from evaluation_backend.storage import NotFound
                raise NotFound('Legacy run')
            assert (collection, project, ident) == ('experiments', 'p', 'exp-1')
            return {'status': 'RUNNING'}
    class Service:
        storage = Store()
        def report_samples(self, project, ident):
            assert (project, ident) == ('p', 'exp-1')
            return [{'id': 'sample-1'}, {'id': 'sample-2'}]

        def dataset_summary(self, experiment, context, samples, *, latencies=None):
            assert experiment['status'] == 'RUNNING' and context is None and len(samples) == 2
            latencies.extend([5, 40])
            return []
        def page(self, collection, project, **kwargs):
            assert collection == 'samples' and project == 'p'
            assert kwargs['filters'] == {'evaluation_id': 'exp-1'}
            return {'items': [{'id': 'sample-1'}], 'total': 2}
    value = report('exp-1', 'p', 0, 1, Service())
    assert value['latency'] == {'average_ms': 22.5, 'p50_ms': 5, 'p95_ms': 40, 'count': 2}
    assert value['partial'] and value['nextOffset'] == 1
    assert value['results'] == [{'id': 'sample-1'}]
    summary = report('exp-1', 'p', 0, 1, Service(), summary_only=True)
    assert summary['results'] == [] and summary['nextOffset'] is None
    assert summary['total'] == 2 and summary['latency'] == value['latency']

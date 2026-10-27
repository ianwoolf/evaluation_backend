"""Real Elasticsearch contracts for shared indices; isolated temporary indices."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from evaluation_backend.storage import Conflict, NotFound
from evaluation_backend.storage.elastic import ElasticsearchStorage
from evaluation_backend.storage.migrate_v2 import migrate


@pytest.fixture
def store():
    if not os.getenv('EVAL_TEST_ES_URL'):
        pytest.skip('Real ES endpoint not selected')
    storage = ElasticsearchStorage(os.environ['EVAL_TEST_ES_URL'], prefix='eval-test-' + uuid.uuid4().hex)
    storage.initialize()
    try:
        yield storage
    finally:
        # Only this test's randomly named indices are removed.
        for index in set(storage.indices.values()) | {f'{storage.prefix}-{c}-v1' for c in
                ('datasets', 'environments', 'target-configs', 'run-contexts', 'experiments', 'samples')}:
            try:
                storage.call('DELETE', '/' + index)
            except NotFound:
                pass


def test_run_context_and_summary_share_document_and_merge_concurrently(store):
    context = {'id': 'r', 'environment': {'modelId': 'none'}, 'request_hash': 'pinned',
               'dataset_id': 'd', 'created_at': '2026-10-26T00:00:00Z'}
    store.put('run-contexts', 'p', 'r', context, create=True)
    assert store.get('experiments', 'p', 'r')['status'] == 'RUNNING'
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(store.put, collection, 'p', 'r', value) for collection, value in (
            ('run-contexts', {**context, 'environment': {'modelId': 'updated'}}),
            ('experiments', {'status': 'COMPLETED', 'items': 2, 'errors': 0, 'created_at': '2026-10-27T00:00:00Z'}))]
        for future in futures:
            future.result()
    result = store.get('experiments', 'p', 'r')
    assert result['environment']['modelId'] == 'updated'
    assert result['status'] == 'COMPLETED' and result['items'] == 2
    assert result['created_at'] == context['created_at']
    assert store.search('experiments', 'p')['total'] == 1
    assert store.search('run-contexts', 'p')['total'] == 1
    with pytest.raises(Conflict):
        store.put('run-contexts', 'p', 'r', context, create=True)
    with pytest.raises(NotFound):
        store.get('experiments', 'other', 'r')


def test_resource_versions_pagination_type_isolation_and_conflict(store):
    dataset = {'id': 'same', 'draft': {'name': 'Draft', 'rows': []},
               'versions': [{'version': 1, 'name': 'First', 'rows': [{'x': 1}]}]}
    saved = store.put('datasets', 'p', 'same', dataset, create=True)
    store.put('environments', 'p', 'same', {'id': 'same', 'versions': [{'version': 1, 'modelId': 'none'}]}, create=True)
    assert store.get('datasets', 'p', 'same')['versions'] == dataset['versions']
    assert store.search('datasets', 'p')['total'] == 1
    assert store.search('environments', 'p')['total'] == 1
    changed = {**dataset, 'draft': {'name': 'Updated'}, 'versions': [*dataset['versions'], {'version': 2, 'name': 'Second'}]}
    store.put('datasets', 'p', 'same', changed, revision=saved['_revision'])
    with pytest.raises(Conflict):
        store.put('datasets', 'p', 'same', dataset, revision=saved['_revision'])
    assert len(store.get('datasets', 'p', 'same')['versions']) == 2
    config = store.put('target-configs', 'p', 'same', {'dataset_id': 'same'}, create=True)
    config = store.put('target-configs', 'p', 'same', {'dataset_id': 'new'}, revision=config['_revision'])
    assert config['version'] == 2
    assert store.get('target-configs', 'p', 'same')['dataset_id'] == 'new'


def test_samples_have_embedded_queryable_scores_and_trace_reference(store):
    trace_id = '["r", "s"]'
    store.put('traces', 'p', trace_id, {'id': trace_id, 'evaluation_id': 'r', 'data': {'trace': [{'kind': 'SCORER'}]}})
    sample = {'id': 's', 'sample_id': 's', 'evaluation_id': 'r', 'input': {'arbitrary': 'safe'},
              'output': {}, 'reference': {}, 'status': 'PASS', 'latencyMs': 12,
              'scores': [{'scorerId': 'a', 'value': 1, 'passed': True, 'reason': 'ok'},
                         {'scorerId': 'b', 'value': 0, 'passed': False, 'reason': 'no'}], 'trace': []}
    for _ in range(2):
        store.put('samples', 'p', trace_id, sample)
    page = store.search('samples', 'p', filters={'evaluation_id': 'r'})
    assert page['total'] == 1 and page['items'][0]['trace'] == [{'kind': 'SCORER'}]
    source = store.call('GET', f'/{store.indices["samples"]}/_doc/{store.record_key("samples", "p", trace_id)}')['_source']
    assert 'payload' not in source and 'trace' not in source and source['latency_ms'] == 12
    assert source['scores'][0]['scorer_id'] == 'a'
    # Nested mapping must not match scorer a with scorer b's score.
    query = {'query': {'nested': {'path': 'scores', 'query': {'bool': {'filter': [
        {'term': {'scores.scorer_id': 'a'}}, {'term': {'scores.value': 0}}]}}}}}
    assert store.call('POST', '/' + store.indices['samples'] + '/_search', json=query)['hits']['total']['value'] == 0


def test_migration_is_repeatable_and_preserves_existing_data(store):
    values = {'datasets': {'id': 'd', 'draft': {'name': 'Old'}, 'versions': []},
              'run-contexts': {'id': 'r', 'request_hash': 'pinned', 'environment': {'modelId': 'none'}},
              'experiments': {'id': 'r', 'status': 'COMPLETED', 'items': 1, 'errors': 0},
              'samples': {'id': 's', 'sample_id': 's', 'evaluation_id': 'r', 'scores': [{'scorerId': 'a', 'value': 1}]}}
    for collection, value in values.items():
        store.call('PUT', f'/{store.prefix}-{collection}-v1/_doc/legacy',
                   json={'project_id': 'p', 'id': value['id'], 'payload': value}, params={'refresh': 'wait_for'})
    first, second = migrate(store, apply=True), migrate(store, apply=True)
    assert first['experiments']['created'] == 1
    assert second['experiments']['created'] == 0
    assert store.get('experiments', 'p', 'r')['request_hash'] == 'pinned'
    assert store.get('experiments', 'p', 'r')['status'] == 'COMPLETED'
    assert store.call('GET', f'/{store.prefix}-datasets-v1/_count')['count'] == 1

import httpx
import pytest
from evaluation_backend.storage.elastic import ElasticsearchStorage
from evaluation_backend.storage import Conflict
from evaluation_backend.storage.factory import create_storage, register_storage


def test_elasticsearch_project_filter_and_disabled_payload_mapping():
    calls = []
    def handle(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        if request.url.path.endswith('_search'):
            return httpx.Response(200, json={'hits': {'hits': [], 'total': {'value': 0}}})
        return httpx.Response(200, json={'acknowledged': True})
    storage = ElasticsearchStorage('http://es.test', client=httpx.Client(transport=httpx.MockTransport(handle)))
    storage.initialize()
    assert all(c['mappings']['properties']['payload']['enabled'] is False for c in calls)
    storage.search('datasets', 'project-a')
    assert calls[-1]['query']['bool']['filter'] == [{'term': {'project_id': 'project-a'}}]
    assert storage.key('a', 'same') != storage.key('b', 'same')


def test_elasticsearch_conflicts_do_not_leak_upstream_body():
    storage = ElasticsearchStorage('http://es.test', client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(409, json={'error': 'private vendor data'}))))
    with pytest.raises(Conflict) as caught:
        storage.put('datasets', 'a', 'd', {}, revision='1:2')
    assert 'private' not in str(caught.value)


def test_custom_adapter_can_be_selected_without_vendor_imports():
    sentinel = object()
    register_storage('test-custom', lambda: sentinel)
    assert create_storage('test-custom') is sentinel
    with pytest.raises(ValueError):
        create_storage('not-registered')

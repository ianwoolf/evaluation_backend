import httpx
import pytest
from evaluation_backend.storage.elastic import ElasticsearchStorage
from evaluation_backend.storage import Conflict
from evaluation_backend.storage.factory import create_storage, register_storage


def test_elasticsearch_project_and_type_filters_and_three_indices():
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
    assert len(calls) == 3
    assert 'payload' not in calls[0]['mappings']['properties']
    assert calls[0]['mappings']['properties']['spec']['enabled'] is False
    assert calls[1]['mappings']['properties']['scores']['type'] == 'nested'
    assert calls[2]['mappings']['properties']['payload']['enabled'] is False
    storage.search('datasets', 'project-a')
    assert calls[-1]['query']['bool']['filter'] == [{'term': {'project_id': 'project-a'}},
        {'term': {'record_type': 'dataset'}}, {'term': {'state': 'head'}}]
    assert storage.key('a', 'same') != storage.key('b', 'same')
    assert storage.record_key('datasets', 'a', 'same') != storage.record_key('environments', 'a', 'same')
    assert storage.record_key('experiments', 'a', 'same') == storage.record_key('run-contexts', 'a', 'same')


def test_index_configuration(monkeypatch):
    monkeypatch.setenv('EVAL_ES_RECORDS_INDEX', 'custom-records-v2')
    monkeypatch.setenv('EVAL_ES_SAMPLES_INDEX', 'custom-samples-v2')
    monkeypatch.setenv('EVAL_ES_TRACES_INDEX', 'custom-traces-v1')
    storage = ElasticsearchStorage.from_env()
    assert storage.index('datasets') == storage.index('experiments') == 'custom-records-v2'
    assert storage.index('samples') == 'custom-samples-v2'
    with pytest.raises(ValueError):
        ElasticsearchStorage('http://es.test', records_index='Bad/Name')
    with pytest.raises(ValueError):
        ElasticsearchStorage('http://es.test', records_index='same', samples_index='same')


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


def test_es_basic_auth_and_api_key_are_sent_without_url_credentials(capsys):
    import base64
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'ok': True})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    storage = ElasticsearchStorage('http://es.test', username='elastic', password='secret:with spaces', client=client)
    assert storage.call('GET', '/') == {'ok': True}
    assert calls[0].headers['Authorization'] == 'Basic ' + base64.b64encode(b'elastic:secret:with spaces').decode()
    assert calls[0].url.userinfo == b''
    assert 'secret:with spaces' not in capsys.readouterr().out
    storage = ElasticsearchStorage('http://es.test', api_key='test-api-key', client=client)
    # Switching explicit auth modes must not leave Basic Auth on an injected client.
    storage.call('GET', '/')
    assert calls[-1].headers['Authorization'] == 'ApiKey test-api-key'


@pytest.mark.parametrize('kwargs', [dict(username='elastic'), dict(password='secret'),
    dict(api_key='key', username='elastic', password='secret'), dict(username='bad:user', password='secret')])
def test_es_rejects_incomplete_or_ambiguous_auth(kwargs):
    with pytest.raises(ValueError):
        ElasticsearchStorage('http://es.test', **kwargs)


def test_es_password_auth_from_environment(monkeypatch):
    monkeypatch.setenv('EVAL_ES_USERNAME', 'elastic')
    monkeypatch.setenv('EVAL_ES_PASSWORD', 'test-environment-password')
    monkeypatch.delenv('EVAL_ES_API_KEY', raising=False)
    storage = ElasticsearchStorage.from_env()
    assert isinstance(storage.client.auth, httpx.BasicAuth)
    storage.client.close()

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


def test_trace_search_returns_groups_then_only_selected_spans():
    import json
    calls=[]
    def handle(request):
        body=json.loads(request.content)
        calls.append(body)
        if 'aggs' in body:
            return httpx.Response(200,json={'aggregations': {'groups': {'buckets': [
                {'key': {'trace_id': 'trace-a'}, 'doc_count': 3,
                 'example': {'hits': {'hits': [{'_source': {'sample_id': 'sample-a'}}]}}}],
                 'after_key': {'trace_id': 'trace-a'}}}})
        return httpx.Response(200,json={'hits': {'total': {'value': 1}, 'hits': [
            {'_source': {'payload': {'id': 'run:trace-a:span-1', 'data': {'span': {'name': 'request'}}}},
             '_seq_no': 1, '_primary_term': 1}]}})
    storage=ElasticsearchStorage('http://es.test',client=httpx.Client(transport=httpx.MockTransport(handle)))
    page=storage.trace_groups('project','run',search='sample-*',after='trace-before')
    assert page == {'items': [{'trace_id':'trace-a','sample_id':'sample-a','span_count':3}], 'next_cursor':None}
    query=calls[0]
    assert query['size']==0
    assert query['query']['bool']['filter'][:2]==[{'term':{'project_id':'project'}},{'term':{'evaluation_id':'run'}}]
    assert query['aggs']['groups']['composite']['after']=={'trace_id':'trace-before'}
    assert '\\*' in query['query']['bool']['filter'][2]['bool']['should'][0]['wildcard']['sample_id']['value']
    selected=storage.trace_group('project','run','trace-a')
    assert selected['items'][0]['data']['span']['name']=='request'
    assert {'prefix': {'id': 'run:trace-a:'}} in calls[1]['query']['bool']['filter']


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


def test_scorer_specs_roundtrip_and_search_isolation():
    import json
    calls = []
    def handle(request):
        body=json.loads(request.content) if request.content else {}
        calls.append(body)
        if request.url.path.endswith('_search'):
            return httpx.Response(200,json={'hits':{'hits':[],'total':{'value':0}}})
        return httpx.Response(200,json={'_seq_no':1,'_primary_term':1})
    storage=ElasticsearchStorage('http://es.test',client=httpx.Client(transport=httpx.MockTransport(handle)))
    spec={'id':'s','source':'CUSTOM','enabled':True,'draft':{'name':'Grounding','criteria':{'expectation':'Grounded'}},'versions':[]}
    storage.put('scorers','project-a','s',spec,create=True)
    doc=calls[-1]
    assert doc['record_type']=='scorer' and doc['spec']==spec
    assert storage.decode('scorers',{'_source':doc,'_seq_no':1,'_primary_term':1})=={**spec,'_revision':'1:1'}
    storage.search('scorers','project-a')
    assert calls[-1]['query']['bool']['filter']==[{'term':{'project_id':'project-a'}},{'term':{'record_type':'scorer'}},{'term':{'state':'head'}}]


def test_sample_page_filters_historical_source_and_pairs_binding_outcome():
    import json
    queries = []
    def handle(request):
        queries.append(json.loads(request.content))
        return httpx.Response(200, json={'hits': {'hits': [], 'total': {'value': 213}}})
    storage = ElasticsearchStorage('http://es.test', client=httpx.Client(transport=httpx.MockTransport(handle)))
    result = storage.search('samples', 'p', filters={'evaluation_id':'run', 'dataset_id':'d',
                            'binding_id':'b', 'outcome':'FN', 'strategy':'test'}, offset=100, limit=100)
    query = queries[-1]
    assert result['total'] == 213
    assert query['from'] == 100 and query['size'] == 100
    assert {'term': {'project_id':'p'}} in query['query']['bool']['filter']
    assert {'term': {'run_id':'run'}} in query['query']['bool']['filter']
    assert {'term': {'sample_dataset_id':'d'}} in query['query']['bool']['filter']
    assert query['runtime_mappings']['sample_outcome']['script']['params'] == {'binding':'b'}
    storage.search('samples', 'p', filters={'evaluation_id':'run', 'binding_id':'b', 'outcome':'ERROR'})
    query = queries[-1]
    assert {'term': {'status':'ERROR'}} in query['query']['bool']['filter']
    assert 'sample_binding_id' in query['runtime_mappings']
    assert 'sample_outcome' not in query['runtime_mappings']


def test_sample_page_loads_all_traces_in_one_batch():
    import json
    paths = []
    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith('_search'):
            hits = [{'_source': {'project_id':'p', 'sample_id':str(i), 'trace_id':'t'+str(i)},
                     '_seq_no':1, '_primary_term':1} for i in range(3)]
            return httpx.Response(200, json={'hits':{'hits':hits,'total':{'value':3}}})
        ids = json.loads(request.content)['ids']
        return httpx.Response(200, json={'docs':[
            {'_id':ident,'found':True,'_source':{'project_id':'p','payload':{'data':{'trace':[{'name':'Target'}]}}}}
            for ident in ids]})
    storage = ElasticsearchStorage('http://es.test', client=httpx.Client(transport=httpx.MockTransport(handle)))
    page = storage.search('samples', 'p')
    assert len(paths) == 2 and paths[1].endswith('/_mget')
    assert all(sample['trace'] == [{'name':'Target'}] for sample in page['items'])


def test_column_multiselect_combines_or_with_cross_column_and():
    import json
    queries = []
    def handle(request):
        queries.append(json.loads(request.content))
        return httpx.Response(200, json={'hits': {'hits': [], 'total': {'value': 0}}})
    storage = ElasticsearchStorage('http://es.test', client=httpx.Client(transport=httpx.MockTransport(handle)))
    storage.search('samples', 'p', filters={'evaluation_id':'run', 'columns':{
        'outcome':['TP','TN'], 'status':['PASS'], 'input':['HeLLo'], 'response':['cleared']}})
    query = queries[-1]
    terms = query['query']['bool']['filter']
    assert {'terms':{'column_outcome':['TP','TN']}} in terms
    assert {'terms':{'column_status':['PASS']}} in terms
    assert {'term':{'column_input':True}} in terms
    assert query['runtime_mappings']['column_input']['script']['params'] == {'field':'input','text':'hello'}
    assert query['runtime_mappings']['column_response']['script']['params']['field'] == 'output'


def test_filter_options_are_project_and_run_scoped_and_support_paging():
    import json
    calls=[]
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'aggregations':{'options':{
            'buckets':[{'key':{'value':'200'}}]}}})
    storage = ElasticsearchStorage('http://es.test', client=httpx.Client(transport=httpx.MockTransport(handle)))
    result=storage.sample_filter_options('p','run','http_status',dataset_id='d',search='2',after='199')
    query=calls[-1]
    assert query['size']==0
    assert {'term':{'project_id':'p'}} in query['query']['bool']['filter']
    assert {'term':{'run_id':'run'}} in query['query']['bool']['filter']
    assert query['aggs']['options']['composite']['after']=={'value':'199'}
    assert result=={'items':['200'],'next_cursor':None}


@pytest.mark.parametrize('column',['duration','guardrail_latency','scores'])
@pytest.mark.parametrize('order',['asc','desc'])
def test_sample_numeric_sort_is_global_stable_and_places_missing_last(column,order):
    import json
    queries=[]
    def handle(request):
        queries.append(json.loads(request.content))
        return httpx.Response(200,json={'hits':{'hits':[],'total':{'value':1000}}})
    storage=ElasticsearchStorage('http://es.test',client=httpx.Client(transport=httpx.MockTransport(handle)))
    storage.search('samples','p',filters={'evaluation_id':'run','sort':(column,order)},offset=100,limit=100)
    query=queries[-1]
    assert query['sort']==[{'sample_sort':{'order':order,'missing':'_last'}},{'id':'asc'}]
    assert query['runtime_mappings']['sample_sort']['type']=='double'
    assert query['from']==100 and query['size']==100

import pytest
from fastapi.testclient import TestClient
from evaluation_backend.collector import create_collector
from evaluation_backend.storage.service import get_service


def test_langsmith_is_explicitly_unimplemented(monkeypatch):
    monkeypatch.setenv('EVAL_COLLECTOR_EXPORTER', 'langsmith')
    with pytest.raises(NotImplementedError):
        create_collector()


def test_otlp_json_exports_trace_with_runner_identity(monkeypatch):
    monkeypatch.setenv('EVAL_COLLECTOR_EXPORTER', 'elasticsearch')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'test-token')
    monkeypatch.setenv('EVAL_PROJECT_ID', 'project')
    monkeypatch.setenv('EXPERIMENT_ID', 'exp-1')
    monkeypatch.setenv('EVAL_COLLECTOR_LOCAL', 'true')
    monkeypatch.setenv('EVAL_CONTENT_POLICY', '{"input":"omit"}')
    monkeypatch.setenv('EVAL_TARGET_SNAPSHOT', '{"id":"g","name":"Guardrail","provider":"nemo guardrail","version":2}')
    saved = []
    class Store:
        def put(self, *args):
            saved.append(args)
    class Service:
        storage = Store()
    app = create_collector()
    app.dependency_overrides[get_service] = lambda: Service()
    with TestClient(app) as client:
        payload = {'resourceSpans': [{'scopeSpans': [{'spans': [{'traceId': 'a', 'spanId': 'b', 'name': 'test'}]}]}]}
        assert client.post('/v1/traces', json=payload).status_code == 401
        assert client.post('/v1/traces', json=payload, headers={'Authorization': 'Bearer test-token'}).status_code == 200
    assert saved[0][:3] == ('traces', 'project', 'exp-1:a:b')
    assert saved[0][3]['target']['provider']=='nemo guardrail'


def test_trace_batch_does_not_block_collector_health(monkeypatch):
    import threading,time
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('EVAL_COLLECTOR_EXPORTER','elasticsearch')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN','test-token')
    monkeypatch.setenv('EVAL_PROJECT_ID','project')
    monkeypatch.setenv('EXPERIMENT_ID','exp-1')
    monkeypatch.setenv('EVAL_COLLECTOR_LOCAL','true')
    started=threading.Event();release=threading.Event();saved=[]
    class Store:
        def put_traces(self,project,records):
            started.set();release.wait(3);saved.extend(records)
    class Service: storage=Store()
    app=create_collector();app.dependency_overrides[get_service]=lambda:Service()
    payload={'resourceSpans':[{'scopeSpans':[{'spans':[{'traceId':'a','spanId':str(i)} for i in range(64)]}]}]}
    with TestClient(app) as client, ThreadPoolExecutor(1) as pool:
        future=pool.submit(client.post,'/v1/traces',json=payload,headers={'Authorization':'Bearer test-token'})
        assert started.wait(3)
        try:
            before=time.monotonic()
            assert client.get('/health').status_code==200
            assert time.monotonic()-before<1
        finally:release.set()
        assert future.result().status_code==200
    assert len(saved)==64


def test_es_trace_batch_uses_one_refresh_and_reports_item_errors():
    import httpx,json
    from evaluation_backend.storage.elastic import ElasticsearchStorage
    from evaluation_backend.storage import StorageError
    calls=[];fail=False
    def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'errors':fail,'items':[{'index':{'status':400 if fail else 201}}]})
    store=ElasticsearchStorage('http://es:9200',client=httpx.Client(transport=httpx.MockTransport(respond)))
    records=[{'id':str(i),'evaluation_id':'exp','data':{'span':{'name':'test'}}} for i in range(64)]
    store.put_traces('p',records)
    assert len(calls)==1 and calls[0].url.path.endswith('/_bulk')
    assert calls[0].url.params['refresh']=='wait_for'
    lines=calls[0].content.decode().splitlines()
    assert len(lines)==128 and json.loads(lines[1])['project_id']=='p'
    fail=True
    with pytest.raises(StorageError):store.put_traces('p',records)


def test_runner_feedback_timeout_is_five_minutes():
    from evaluation_backend.storage.client import StorageClient
    c=StorageClient(env={'EVAL_STORAGE_API_URL':'http://backend:8001','EVAL_STORAGE_TOKEN':'test','EVAL_PROJECT_ID':'p'})
    try:assert c.client.timeout.read==300
    finally:c.client.close()

import importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from evaluation_backend.mock_guardrail import app


def test_scan_and_scorer(monkeypatch):
    draws = iter([0.75, 0.75, 0.25, 0.25])
    monkeypatch.setattr('evaluation_backend.mock_guardrail.random.random', lambda: next(draws))
    monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN', 'local-test-token')
    c=TestClient(app); path='/runtime/v1/endpoints/mock-guardrail/backend/v1/scans'
    assert c.post(path,json={'input':'hi'}).status_code==401
    h={'Authorization':'Bearer local-test-token'}
    spec=importlib.util.spec_from_file_location('fixture_scorer',Path(__file__).parents[1]/'examples/guardrail-scorer/scorer.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for text,label,expected in [('hi','safe','TN'),('hi','unsafe','FN'),('[mock:unsafe]','safe','FP'),('[mock:unsafe]','unsafe','TP')]:
        r=c.post(path,headers=h,json={'input':text,'verbose':True});assert r.status_code==200
        assert module.score({'label':label,'output':r.json()})['outcome']==expected
        assert len(r.json()['result']['scannerResults'])==1
    with pytest.raises(ValueError):module.score({'label':'safe','output':{'result':{'outcome':'unknown'}}})

def test_structured_request_logs_and_health_filter(monkeypatch,capsys):
    import json
    monkeypatch.setattr('evaluation_backend.mock_guardrail.random.random', lambda: 0.25)
    monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN','DO_NOT_LOG_TOKEN')
    client=TestClient(app)
    for path in ('/health','/ready','/health?probe=1'):
        assert client.get(path).status_code==200
    assert capsys.readouterr().out==''
    path='/runtime/v1/endpoints/mock-guardrail/backend/v1/scans'
    headers={'Authorization':'Bearer DO_NOT_LOG_TOKEN','X-Request-ID':'accept-123','traceparent':'00-'+'a'*32+'-'+'b'*16+'-01'}
    r=client.post(path,headers=headers,json={'input':'[mock:unsafe] PRIVATE_INPUT'})
    assert r.status_code==200 and r.json()['id']==r.headers['X-Request-ID']=='accept-123'
    raw=capsys.readouterr().out;rows=[json.loads(line) for line in raw.splitlines()]
    assert [x['event'] for x in rows]==['guardrail.request.started','guardrail.request.completed']
    assert rows[1]['outcome']=='flagged' and rows[1]['trace_id']=='a'*32
    assert rows[1]['status_code']==200 and rows[1]['duration_ms']>=0
    assert 'DO_NOT_LOG_TOKEN' not in raw and 'PRIVATE_INPUT' not in raw
    for payload,auth,status in [({'input':'hi'},False,401),({},True,422)]:
        r=client.post(path,headers=headers if auth else {},json=payload)
        assert r.status_code==status
        rows=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert rows[-1]['event']=='guardrail.request.failed' and rows[-1]['status_code']==status
        assert rows[0]['request_id']==rows[-1]['request_id']==r.headers['X-Request-ID']


@pytest.mark.parametrize('draw,expected',[(0.499,'blocked'),(0.5,'cleared')])
def test_random_outcome_is_independent_of_input(monkeypatch,draw,expected):
    monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN','test-token')
    monkeypatch.setattr('evaluation_backend.mock_guardrail.random.random',lambda:draw)
    c=TestClient(app);path='/runtime/v1/endpoints/mock-guardrail/backend/v1/scans';h={'Authorization':'Bearer test-token'}
    for text in ('benign', '[mock:unsafe]', '[mock:timeout]', '[mock:error]'):
        result=c.post(path,headers=h,json={'input':text,'flagOnly':False})
        assert result.status_code==200
        assert result.json()['result']['outcome']==expected

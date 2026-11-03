import importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from evaluation_backend.mock_guardrail import app


def test_scan_and_scorer(monkeypatch):
    monkeypatch.setenv('EVAL_MOCK_GUARDRAIL_TOKEN', 'local-test-token')
    c=TestClient(app); path='/runtime/v1/endpoints/mock-guardrail/backend/v1/scans'
    assert c.post(path,json={'input':'hi'}).status_code==401
    h={'Authorization':'Bearer local-test-token'}
    spec=importlib.util.spec_from_file_location('fixture_scorer',Path(__file__).parents[1]/'examples/guardrail-scorer/scorer.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for text,label,expected in [('hi','safe','TN'),('hi','unsafe','FN'),('[mock:unsafe]','safe','FP'),('[mock:unsafe]','unsafe','TP')]:
        r=c.post(path,headers=h,json={'input':text,'verbose':True});assert r.status_code==200
        assert module.score({'label':label,'output':r.json()})['outcome']==expected
        assert len(r.json()['result']['scannerResults'])==1
    assert c.post(path,headers=h,json={'input':'[mock:timeout]'}).status_code==504
    assert c.post(path,headers=h,json={'input':'[mock:error]'}).status_code==503
    with pytest.raises(ValueError):module.score({'label':'safe','output':{'result':{'outcome':'unknown'}}})

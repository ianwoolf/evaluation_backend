import copy,json
import pytest
from fastapi.testclient import TestClient
from evaluation_backend.api import create_app
from evaluation_backend.runs import kube
from evaluation_backend.storage import NotFound, Conflict
from evaluation_backend.storage.service import EvaluationStorageService,get_service
from evaluation_backend.uploads import get_upload_store,UploadStore
from test_uploads import SCHEMA,CSV
from test_runs import Cluster

from memory_storage import Memory

@pytest.fixture
def live(monkeypatch,tmp_path):
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN','secret')
    monkeypatch.setenv('EVAL_WORKER_IMAGE','worker:test')
    monkeypatch.setenv('EVAL_UPLOAD_DIR',str(tmp_path))
    monkeypatch.setenv('EVAL_CONTENT_POLICY','{"input":"omit","response":"omit","reference":"omit"}')
    binding={'target_id':'g','dataset_id':'d','dataset_version':1,'environment_id':'e','model_id':'m'}
    mem=Memory();mem.put('datasets','p','d',{'id':'d','versions':[SCHEMA]})
    mem.put('environments','p','e',{'id':'e','versions':[{'name':'Isolated','version':1,'cpuMillicores':750,'memoryMiB':768,'modelId':'m','guardrailId':'g'}]})
    mem.put('targets','p','g',{'id':'g','name':'Guardrail','kind':'GUARDRAIL','provider':'nemo guardrail','version':2,'definition':''})
    app=create_app();service=EvaluationStorageService(mem);cluster=Cluster()
    app.dependency_overrides[get_service]=lambda:service
    app.dependency_overrides[kube]=lambda:cluster
    headers={'Authorization':'Bearer secret','X-Project-ID':'p','Idempotency-Key':'upload-test-123456789'}
    body={'name':'Test',**binding,'scoring_mode':'mock','scorer_ids':['mock-score'],'snapshot':{'dataset':{'rows':[{'secret':'CANARY'}]}}}
    return TestClient(app),mem,cluster,headers,body


def test_upload_validate_launch_scoped_read_policy_and_cleanup(live):
    c,mem,k,h,b=live
    u=c.post('/api/v1/evaluations/uploads?dataset_id=d&dataset_version=1',content=CSV,headers={**h,'X-Upload-Filename':'cases.csv','Content-Type':'application/octet-stream'})
    assert u.status_code==200,u.text
    b['upload_id']=u.json()['id']
    r=c.post('/api/v1/evaluations/runs',json=b,headers=h)
    assert r.status_code==200,r.text
    run=r.json();job=k.saved[run['id']]
    assert 'CANARY' not in json.dumps(job)
    assert not run['snapshot']['dataset']['rows']
    assert run['sample_count']==1
    env={v['name']:v['value'] for v in job['spec']['template']['spec']['containers'][0]['env'] if 'value' in v}
    assert json.loads(env['EVAL_CONTENT_POLICY'])['input']=='omit'
    wh={'Authorization':'Bearer '+env['EVAL_STORAGE_TOKEN'],'X-Project-ID':'p'}
    path='/api/v1/evaluations/run-input/'+run['experiment_id']
    assert c.get(path,headers=wh).json()['rows'][0]['messages'][0]['content']=='hello'
    assert c.get(path,headers={**wh,'X-Project-ID':'other'}).status_code==403
    assert c.get('/api/v1/evaluations/run-input/exp-other',headers=wh).status_code==403
    assert c.post('/api/v1/evaluations/runs',json=b,headers=wh).status_code==403
    assert c.post('/api/v1/evaluations/runs',json=b,headers=h).json()['id']==run['id']
    k.saved[run['id']]['status']={'conditions':[{'type':'Failed','status':'True'}]}
    c.get('/api/v1/evaluations/runs/'+run['id'],headers=h)
    assert c.get(path,headers=wh).status_code==410
    assert c.post('/api/v1/evaluations/runs',json=b,headers=h).json()['id']==run['id']


def test_api_filters_samples_and_traces_against_frozen_run_policy(live):
    c,mem,k,h,b=live
    run=c.post('/api/v1/evaluations/runs',json=b,headers=h).json();exp=run['experiment_id']
    sample={'id':'s1','input':{'text':'CANARY'},'output':{'text':'CANARY'},'reference':{'text':'CANARY'},'status':'PASS','latencyMs':12,'scores':[{'scorerId':'mock-score','value':1,'passed':True,'reason':'CANARY'}],'trace':[]}
    assert c.post('/api/v1/evaluations/experiments/'+exp+'/samples',json=sample,headers=h).status_code==200
    assert c.post('/api/v1/evaluations/traces',json={'id':'t1','evaluation_id':exp,'name':'CANARY','data':{'input':'CANARY','metadata':{'raw':'CANARY'}}},headers=h).status_code==200
    assert 'CANARY' not in json.dumps(mem.search('samples','p'))
    assert 'CANARY' not in json.dumps(mem.search('scores','p'))
    assert 'CANARY' not in json.dumps(mem.search('traces','p'))
    assert all(t['target']['provider']=='nemo guardrail' for t in mem.search('traces','p')['items'])


def test_upload_errors_do_not_create_jobs_and_client_cannot_override_policy(live):
    c,mem,k,h,b=live
    r=c.post('/api/v1/evaluations/uploads?dataset_id=d&dataset_version=1',content=b'messages,has_issue\nCANARY,true',headers={**h,'X-Upload-Filename':'cases.csv'})
    assert r.status_code==422 and 'CANARY' not in r.text and k.created==0
    b['content_policy']={'input':'retain'}
    assert c.post('/api/v1/evaluations/runs',json=b,headers=h).status_code==422
    assert k.created==0


def test_policy_remains_frozen_when_server_default_changes(live,monkeypatch):
    c,mem,k,h,b=live
    first=c.post('/api/v1/evaluations/runs',json=b,headers=h).json()
    monkeypatch.setenv('EVAL_CONTENT_POLICY','{}')
    sample={'id':'s','input':{'text':'CANARY'},'output':{},'reference':{},'status':'PASS','latencyMs':1,'scores':[],'trace':[]}
    c.post('/api/v1/evaluations/experiments/'+first['experiment_id']+'/samples',json=sample,headers=h)
    assert 'CANARY' not in json.dumps(mem.search('samples','p'))


def test_terminal_reconciler_removes_upload_without_report_polling(live):
    c,mem,k,h,b=live
    u=c.post('/api/v1/evaluations/uploads?dataset_id=d&dataset_version=1',content=CSV,headers={**h,'X-Upload-Filename':'cases.csv'}).json()
    b['upload_id']=u['id'];r=c.post('/api/v1/evaluations/runs',json=b,headers=h).json()
    get_upload_store().reconcile(lambda job:job==r['id'])
    assert c.get('/api/v1/evaluations/run-input/'+r['experiment_id'],headers=h).status_code==410

import copy
import threading
import time
import pytest
from evaluation_backend import guardrail_worker, git_scorers

@pytest.mark.parametrize('concurrency',[1,3])
def test_global_case_pool_has_bounded_parallelism_and_concurrent_writers(monkeypatch,concurrency):
    monkeypatch.setenv('EVAL_CONCURRENCY',str(concurrency))
    monkeypatch.setenv('EXPERIMENT_ID','test-run')
    owner=threading.get_ident(); active=0; peak=0; lock=threading.Lock(); closed=[]
    write_active=0; write_peak=0
    writers=threading.Barrier(concurrency)
    class Http:
        def __init__(self,**kwargs): assert kwargs['verify'] is False
        def post(self,url,**kwargs):
            nonlocal active,peak
            with lock: active+=1;peak=max(active,peak)
            time.sleep(.03)
            with lock: active-=1
            import httpx
            status=500 if kwargs['json']['input']=='error' else 200
            return httpx.Response(status,json={'status':'cleared'},request=httpx.Request('POST',url))
        def close(self): closed.append(self)
    class Client:
        def __init__(self): self.samples=[];self.summaries=[]
        def run_input(self,ident):
            return {'datasets':[{'dataset_id':d,'version':1,'name':d,'rows':[{'input':'error' if d=='b' and i==0 else 'hello','label':0,'strategy':'benign'} for i in range(6)]} for d in ['a','b']],
                    'guardrail_bindings':[{'dataset_id':d,'binding_id':d,'label_mapping':{'0':'safe'},'scorer_execution':{}} for d in ['a','b']]}
        def call(self,*args):return {'endpoint':'https://mock/scan','token':'fake','verify_ssl':False}
        def sample(self,ident,value):
            nonlocal write_active,write_peak
            assert threading.get_ident()!=owner
            with lock:
                write_active+=1
                write_peak=max(write_peak,write_active)
            writers.wait(timeout=5)
            with lock:
                self.samples.append(copy.deepcopy(value))
                write_active-=1
        def experiment(self,ident,value):
            assert threading.get_ident()==owner
            self.summaries.append(copy.deepcopy(value))
    monkeypatch.setattr(guardrail_worker.httpx,'Client',Http)
    monkeypatch.setattr(git_scorers,'prepare',lambda config:None)
    def evaluate(config, evidence):
        response=evidence['target_response']
        assert response['http_code'] in (200,500)
        assert response['body']=={'status':'cleared'}
        if response['http_code']==500:
            return {'state_type':'error','guardrail_outcome':None,'reason':'Guardrail returned HTTP 500'}
        return {'state_type':'ok','guardrail_outcome':'cleared','expected':'safe','predicted':'safe','outcome':'TN','reason':'test'}
    monkeypatch.setattr(git_scorers,'execute',evaluate)
    client=Client();summary=guardrail_worker.run(client)
    assert peak==concurrency
    assert write_peak==concurrency
    assert len(client.samples)==len({s['id'] for s in client.samples})==12
    assert summary['items']==12 and summary['errors']==1 and summary['status']=='COMPLETED'
    assert [d['items'] for d in summary['dataset_results']]==[6,6]
    assert [d['errors'] for d in summary['dataset_results']]==[0,1]
    assert [d['status'] for d in summary['dataset_results']]==['COMPLETED','COMPLETED']
    failed=[sample for sample in client.samples if sample['status']=='ERROR']
    assert len(failed)==1 and failed[0]['guardrail_result']['http_status']==500
    assert failed[0]['guardrail_result']['state_type']=='error'
    assert failed[0]['error']=='Guardrail returned HTTP 500'
    assert all(s['caseid'].startswith('row-') and s['caseid']==s['guardrail_result']['caseid'] for s in client.samples)
    assert client.summaries[-1]==summary
    assert len(closed)==concurrency


def test_guardrail_request_failure_logs_reason_without_header_value(capsys,monkeypatch):
    import json
    import httpx

    class Http:
        def post(self, *args, **kwargs):
            raise httpx.LocalProtocolError("Illegal header value b'Bearer SECRET_CANARY'")

    content = {'dataset_id': 'd', 'version': 1}
    binding = {'binding_id': 'b', 'label_mapping': {'0': 'safe'}, 'scorer_execution': {}}
    row = {'input': 'INPUT_CANARY', 'label': 0, 'strategy': 'benign'}
    detail = {'endpoint': 'https://guard.test/scan', 'token': 'SECRET_CANARY', 'verify_ssl': False}
    def evaluate(config, evidence):
        response=evidence['target_response']
        assert response['http_code'] is None and response['client_timeout'] is False
        return {'state_type':'error','guardrail_outcome':None,'reason':response['error']}
    from evaluation_backend import git_scorers
    monkeypatch.setattr(git_scorers,'execute',evaluate)
    _, sample = guardrail_worker.execute_case('r', 0, 0, content, binding, row, Http(), detail)
    output = capsys.readouterr().out
    assert 'SECRET_CANARY' not in output and 'INPUT_CANARY' not in output
    fields = json.loads(output.splitlines()[-1])
    assert fields['event'] == 'guardrail.sample.failed'
    assert sample['status'] == 'ERROR'
    assert sample['guardrail_result']['reason'] == 'Invalid HTTP header value'
    assert fields['error_reason'] == 'Invalid HTTP header value'
    assert fields['server_host'] == 'guard.test' and fields['verify_ssl'] is False


@pytest.mark.parametrize('status,expected',[('timeout','timeout'),(408,'timeout'),(429,'rate-limit'),(499,'timeout'),(503,'error')])
def test_target_failures_are_scored_and_keep_case_reason(monkeypatch,status,expected):
    import httpx
    import runpy

    score=runpy.run_path('scripts/git-scorer-fixtures/scan_classification.py')['score']
    seen=[]
    def evaluate(config,evidence):
        seen.append(copy.deepcopy(evidence))
        return score(evidence)
    monkeypatch.setattr(git_scorers,'execute',evaluate)
    class Http:
        def post(self,url,**kwargs):
            if status=='timeout':raise httpx.ReadTimeout('expired')
            return httpx.Response(status,json={'result':{'outcome':'blocked'}},request=httpx.Request('POST',url))
    content={'dataset_id':'d','version':1}
    binding={'binding_id':'b','label_mapping':{'unsafe':'unsafe'},'scorer_execution':{}}
    row={'caseid':'case-42','input':'unsafe input','label':'unsafe','strategy':'test'}
    _,sample=guardrail_worker.execute_case('r',0,0,content,binding,row,Http(),{'endpoint':'https://guard.test/scan','token':'token'})
    assert len(seen)==1
    assert seen[0]['caseid']=='case-42'
    assert seen[0]['target_response']['client_timeout']==(status=='timeout')
    assert seen[0]['target_response']['http_code']==(None if status=='timeout' else status)
    assert sample['caseid']==sample['guardrail_result']['caseid']=='case-42'
    assert sample['guardrail_result']['state_type']==expected
    assert sample['guardrail_result']['guardrail_outcome']==(None if status=='timeout' else 'blocked')
    assert sample['status']=='ERROR' and sample['error'] and sample['guardrail_result']['reason']==sample['error']
    assert sample['guardrail_result']['classifications']==[]


def test_evaluator_exception_is_persisted_as_case_reason(monkeypatch):
    import httpx
    class Http:
        def post(self,url,**kwargs):
            return httpx.Response(200,json={'result':{'outcome':'cleared'}},request=httpx.Request('POST',url))
    def broken(*args):raise ValueError('private response content')
    monkeypatch.setattr(git_scorers,'execute',broken)
    _,sample=guardrail_worker.execute_case('r',0,0,{'dataset_id':'d','version':1},
        {'binding_id':'b','label_mapping':{'safe':'safe'},'scorer_execution':{}},
        {'input':'hello','label':'safe','strategy':'benign'},Http(),{'endpoint':'https://guard.test/scan','token':'token'})
    assert sample['status']=='ERROR'
    assert sample['guardrail_result']['error_stage']=='scorer'
    assert sample['guardrail_result']['state_type']=='error'
    assert sample['error']=='Evaluator execution failed: ValueError'


def test_current_evaluator_returns_raw_outcome_and_classification(monkeypatch):
    import httpx
    import runpy
    score=runpy.run_path('scripts/git-scorer-fixtures/scan_classification.py')['score']
    monkeypatch.setattr(git_scorers,'execute',lambda config,evidence:score(evidence))
    class Http:
        def post(self,url,**kwargs):
            return httpx.Response(200,json={'result':{'outcome':'blocked'}},request=httpx.Request('POST',url))
    _,sample=guardrail_worker.execute_case('r',0,0,{'dataset_id':'d','version':1},
        {'binding_id':'b','label_mapping':{'unsafe':'unsafe'},'scorer_execution':{}},
        {'caseid':'unsafe-1','input':'hello','label':'unsafe','strategy':'injection'},Http(),
        {'endpoint':'https://guard.test/scan','token':'token'})
    result=sample['guardrail_result']
    assert sample['status']=='PASS' and sample['error'] is None
    assert result['state_type']=='ok' and result['guardrail_outcome']=='blocked'
    assert result['classifications'][0]['outcome']=='TP'
    assert 'label' not in result and sample['reference']['label']=='unsafe'


def test_older_published_evaluator_can_finish_success_and_classify_rate_limit(monkeypatch):
    import httpx
    class Http:
        def __init__(self,status):self.status=status
        def post(self,url,**kwargs):
            return httpx.Response(self.status,json={'result':{'outcome':'cleared'}},request=httpx.Request('POST',url))
    def older(config,evidence):
        if evidence['target_response']['http_code']==429:raise KeyError('output')
        assert evidence['output']['result']['outcome']=='cleared'
        return {'expected':'safe','predicted':'safe','outcome':'TN','reason':'legacy'}
    monkeypatch.setattr(git_scorers,'execute',older)
    args=('r',0,0,{'dataset_id':'d','version':1},{'binding_id':'b','label_mapping':{'safe':'safe'},'scorer_execution':{}},
          {'input':'hello','label':'safe','strategy':'benign'})
    detail={'endpoint':'https://guard.test/scan','token':'token'}
    _,ok=guardrail_worker.execute_case(*args,Http(200),detail)
    _,limited=guardrail_worker.execute_case(*args,Http(429),detail)
    assert ok['status']=='PASS' and ok['guardrail_result']['state_type']=='ok'
    assert limited['status']=='ERROR' and limited['guardrail_result']['state_type']=='rate-limit'
    assert 'evaluator execution failed' in limited['error']

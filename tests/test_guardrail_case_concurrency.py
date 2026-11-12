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
    monkeypatch.setattr(git_scorers,'execute',lambda *args:{'expected':'safe','predicted':'safe','outcome':'TN','reason':'test'})
    client=Client();summary=guardrail_worker.run(client)
    assert peak==concurrency
    assert write_peak==concurrency
    assert len(client.samples)==len({s['id'] for s in client.samples})==12
    assert summary['items']==12 and summary['errors']==1 and summary['status']=='FAILED'
    assert [d['items'] for d in summary['dataset_results']]==[6,6]
    assert [d['errors'] for d in summary['dataset_results']]==[0,1]
    assert len(closed)==concurrency


def test_guardrail_request_failure_logs_reason_without_header_value(capsys):
    import json
    import httpx

    class Http:
        def post(self, *args, **kwargs):
            raise httpx.LocalProtocolError("Illegal header value b'Bearer SECRET_CANARY'")

    content = {'dataset_id': 'd', 'version': 1}
    binding = {'binding_id': 'b', 'label_mapping': {'0': 'safe'}}
    row = {'input': 'INPUT_CANARY', 'label': 0, 'strategy': 'benign'}
    detail = {'endpoint': 'https://guard.test/scan', 'token': 'SECRET_CANARY', 'verify_ssl': False}
    _, sample = guardrail_worker.execute_case('r', 0, 0, content, binding, row, Http(), detail)
    output = capsys.readouterr().out
    assert 'SECRET_CANARY' not in output and 'INPUT_CANARY' not in output
    fields = json.loads(output.splitlines()[-1])
    assert fields['event'] == 'guardrail.sample.failed'
    assert sample['status'] == 'ERROR'
    assert fields['error_reason'] == 'Invalid HTTP header value'
    assert fields['server_host'] == 'guard.test' and fields['verify_ssl'] is False

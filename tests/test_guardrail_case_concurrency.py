import copy
import threading
import time
import pytest
from evaluation_backend import guardrail_worker, git_scorers

@pytest.mark.parametrize('concurrency',[1,3])
def test_global_case_pool_has_bounded_parallelism_and_single_writer(monkeypatch,concurrency):
    monkeypatch.setenv('EVAL_CONCURRENCY',str(concurrency))
    monkeypatch.setenv('EXPERIMENT_ID','test-run')
    owner=threading.get_ident(); active=0; peak=0; lock=threading.Lock(); closed=[]
    class Http:
        def __init__(self,**kwargs): pass
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
        def call(self,*args):return {'endpoint':'http://mock/scan','token':'fake'}
        def sample(self,ident,value):
            assert threading.get_ident()==owner
            self.samples.append(copy.deepcopy(value))
        def experiment(self,ident,value):
            assert threading.get_ident()==owner
            self.summaries.append(copy.deepcopy(value))
    monkeypatch.setattr(guardrail_worker.httpx,'Client',Http)
    monkeypatch.setattr(git_scorers,'prepare',lambda config:None)
    monkeypatch.setattr(git_scorers,'execute',lambda *args:{'expected':'safe','predicted':'safe','outcome':'TN','reason':'test'})
    client=Client();summary=guardrail_worker.run(client)
    assert peak==concurrency
    assert len(client.samples)==len({s['id'] for s in client.samples})==12
    assert summary['items']==12 and summary['errors']==1 and summary['status']=='FAILED'
    assert [d['items'] for d in summary['dataset_results']]==[6,6]
    assert [d['errors'] for d in summary['dataset_results']]==[0,1]
    assert len(closed)==concurrency

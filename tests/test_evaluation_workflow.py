import copy
import pytest
from test_runs import setup
from test_guardrail_contracts import content
from evaluation_backend.storage.service import get_service

ROOT='/api/v1/evaluations'

def prepare(setup, monkeypatch):
 c,k,b,h=setup
 monkeypatch.setenv('EVAL_MOCK_GUARDRAIL_ID','g')
 service=c.app.dependency_overrides[get_service]()
 data=content();data.pop('guardrail');data['scorerIds']=[]
 for ident in ('first','second','outside'):
  payload={'id':ident,'draft':data,'versions':[{**data,'version':1,'publishedAt':'2026-11-03T00:00:00Z'}]}
  r=c.post(ROOT+'/datasets/'+ident,json=payload,headers=h)
  assert r.status_code==200,r.text
 env=service.storage.get('environments','p','e');env['versions'][0]['modelId']='none';service.storage.put('environments','p','e',env)
 execution=dict(engine='git',repo_url='https://github.com/example/scorer.git',script_path='score.py',commit='a'*40,timeout=30)
 service.storage.put('scorers','p','classifier',dict(id='classifier',enabled=True,versions=[dict(version=1,name="Classifier",execution=execution)]))
 refs=[dict(dataset_id=i,dataset_version=1) for i in ('first','second')]
 bindings=[dict(**r,scorer_id='classifier',scorer_version=1,aspect_id='safety',aspect_name='Safety',label_mapping={'0':'safe','1':'unsafe'}) for r in refs]
 config=dict(**refs[0],datasets=refs,guardrail_bindings=bindings,execution_mode='guardrail',environment_id='e',environment_version=1,scoring_mode='installed',scorer_selection='dataset',scorer_ids=[],data_source='published')
 target=c.get(ROOT+'/targets/g',headers=h).json();revision=target.pop('revision')
 return c,k,h,service,dict(target=target,config=config,revision=revision)


def test_dataset_first_atomic_configuration_and_subset(setup,monkeypatch):
 c,k,h,service,body=prepare(setup,monkeypatch)
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h)
 assert saved.status_code==200,saved.text
 config=c.get(ROOT+'/targets/g/config',headers=h).json()
 assert len(config['guardrail_bindings'])==2
 r=c.post(ROOT+'/targets/g/runs',json={'config_revision':config['revision'],'datasets':[{'dataset_id':'second','dataset_version':1}]},headers=h)
 assert r.status_code==200,r.text
 assert r.json()['execution_mode']=='guardrail'
 assert [d['dataset_id'] for d in r.json()['datasets']]==['second']
 context=service.storage.get('run-contexts','p',r.json()['experiment_id'])
 assert context['guardrail_bindings'][0]['scorer_commit']=='a'*40
 assert context['guardrail_bindings'][0]['aspect_name']=='Safety'
 assert context['scoring_selections'][0]['scorer_bindings'][0]['version']=='1'
 assert context['scoring_selections'][0]['scorer_bindings'][0]['definition']['execution']['commit']=='a'*40
 assert k.created==1


def test_scope_and_stale_config_rejected(setup,monkeypatch):
 c,k,h,service,body=prepare(setup,monkeypatch)
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h).json()
 revision=saved['revision']
 for refs in ([{'dataset_id':'outside','dataset_version':1}],[{'dataset_id':'first','dataset_version':2}]):
  r=c.post(ROOT+'/targets/g/runs',json={'config_revision':revision,'datasets':refs},headers=h)
  assert r.status_code==422,r.text
 body['revision']=revision;body['config']['guardrail_bindings'][0]['aspect_name']='Updated'
 updated=c.post(ROOT+'/targets/g/configuration',json=body,headers=h)
 assert updated.status_code==200,updated.text
 assert c.post(ROOT+'/targets/g/runs',json={'config_revision':revision},headers=h).status_code==409
 assert k.created==0


def test_invalid_binding_does_not_partially_save_target(setup,monkeypatch):
 c,k,h,service,body=prepare(setup,monkeypatch)
 original=copy.deepcopy(service.storage.get('targets','p','g'))
 body['target']['name']='Must not save';body['config']['guardrail_bindings'][0]['scorer_version']=99
 assert c.post(ROOT+'/targets/g/configuration',json=body,headers=h).status_code==422
 assert service.storage.get('targets','p','g')==original
 body['config']['guardrail_bindings'][0]['scorer_version']=1
 body['config']['guardrail_bindings'][0]['label_mapping']={'safe':'safe'}
 assert c.post(ROOT+'/targets/g/configuration',json=body,headers=h).status_code==422
 assert service.storage.get('targets','p','g')==original


def test_direct_guardrail_endpoint_requires_saved_configuration(setup,monkeypatch):
 c,k,h,service,body=prepare(setup,monkeypatch)
 value=dict(target_id='g',datasets=[{'dataset_id':'outside','dataset_version':1}],environment_id='e',environment_version=1)
 assert c.post(ROOT+'/guardrail-runs',json=value,headers=h).status_code==422
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h).json()
 value['config_revision']=saved['revision']
 assert c.post(ROOT+'/guardrail-runs',json=value,headers=h).status_code==422
 assert k.created==0

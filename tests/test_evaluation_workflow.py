import copy
from io import BytesIO
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
 r=c.post(ROOT+'/targets/g/runs',json={'config_revision':config['revision'],'datasets':config['datasets'],'provider':'f5','guardrail_id':'g'},headers=h)
 assert r.status_code==200,r.text
 assert r.json()['execution_mode']=='guardrail'
 assert [d['dataset_id'] for d in r.json()['datasets']]==['first','second']
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


@pytest.mark.parametrize('file_type', ['csv', 'xlsx'])
def test_trigger_with_validated_upload_replaces_only_selected_rows(setup, monkeypatch, tmp_path, file_type):
 c,k,h,service,body=prepare(setup,monkeypatch)
 monkeypatch.setenv('EVAL_UPLOAD_DIR', str(tmp_path))
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h)
 assert saved.status_code==200,saved.text
 revision=saved.json()['revision']
 url=f'{ROOT}/uploads?dataset_id=first&dataset_version=1&target_id=g&config_revision={revision}'
 invalid=c.post(url,content=b'input,label,strategy\nexample,9,manual',headers={**h,'X-Upload-Filename':'bad.csv'})
 assert invalid.status_code==422 and 'label_mapping' in invalid.text
 rows=[('file case one',1,'manual'),('file case two',0,'manual')]
 if file_type=='xlsx':
  from openpyxl import Workbook
  book=Workbook();book.active.append(('input','label','strategy'))
  for row in rows:book.active.append(row)
  buffer=BytesIO();book.save(buffer);data=buffer.getvalue()
 else:
  data=('input,label,strategy\n'+'\n'.join(','.join(map(str,row)) for row in rows)).encode()
 uploaded=c.post(url,content=data,headers={**h,'X-Upload-Filename':f'cases.{file_type}'})
 assert uploaded.status_code==200,uploaded.text
 refs=copy.deepcopy(body['config']['datasets']);refs[0]['upload_id']=uploaded.json()['id']
 value=dict(config_revision=revision,datasets=refs,provider='f5',guardrail_id='g')
 run=c.post(ROOT+'/targets/g/runs',json=value,headers=h)
 assert run.status_code==200,run.text
 result=run.json()
 assert result['sample_count']==3 and [d['sample_count'] for d in result['datasets']]==[2,1]
 assert result['datasets'][0]['input_source']=='upload' and 'upload_id' not in result['datasets'][0]
 payload=c.get(ROOT+'/run-input/'+result['experiment_id'],headers=h).json()
 assert [r['input'] for r in payload['datasets'][0]['rows']]==['file case one','file case two']
 assert [r['input'] for r in payload['datasets'][1]['rows']]==['hello']
 assert [b['sample_count'] for b in payload['guardrail_bindings']]==[2,1]
 assert service.dataset_version('p','first',1)['rows'][0]['input']=='hello'
 assert c.post(ROOT+'/targets/g/runs',json=value,headers=h).json()['id']==result['id']
 assert k.created==1


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


def test_run_target_varies_without_changing_evaluation_version(setup,monkeypatch):
 c,k,h,service,body=prepare(setup,monkeypatch)
 body['target']['provider']=None;body['target']['guardrail_id']=None
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h)
 assert saved.status_code==200,saved.text
 config=c.get(ROOT+'/targets/g/config',headers=h).json()
 registry_url=ROOT+'/admin/guardrail-registry'
 registry=c.get(registry_url,headers=h).json()
 registry['registry']['providers'].append({
  'id':'production-guard','name':'Production Guard','instances':[{'id':'g','address':'https://guard.test/scans'}],
 })
 assert c.put(registry_url,json=registry,headers=h).status_code==200
 contexts=[]
 for provider in ('f5','nemo guardrail','production-guard'):
  r=c.post(ROOT+'/targets/g/runs',json={'config_revision':config['revision'],'provider':provider,'guardrail_id':'g'},headers={**h,'Idempotency-Key':'run-provider-'+provider+'-1234567890'})
  assert r.status_code==200,r.text
  contexts.append(service.storage.get('run-contexts','p',r.json()['experiment_id']))
 assert contexts[0]['evaluation_config_version']==contexts[1]['evaluation_config_version']==saved.json()['version']
 assert contexts[0]['datasets']==contexts[1]['datasets']
 assert contexts[0]['guardrail_connection']['provider']=='f5'
 assert contexts[1]['guardrail_connection']['provider']=='nemo guardrail'
 assert contexts[2]['guardrail_connection']['address']=='https://guard.test/scans'
 assert c.post(ROOT+'/targets/g/runs',json={'config_revision':config['revision'],'provider':'f5','guardrail_id':'missing'},headers={**h,'Idempotency-Key':'missing-instance-123456789'}).status_code==422
 assert k.created==3

def test_trigger_concurrency_overrides_share_group_and_keep_environment_default(setup, monkeypatch):
 c,k,h,service,body=prepare(setup,monkeypatch)
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h)
 assert saved.status_code==200,saved.text
 config=c.get(ROOT+'/targets/g/config',headers=h).json()
 expected_default=config['concurrency']
 group='4bdd471a-a54c-460f-a946-d39d09897360'
 base={'config_revision':config['revision'],'provider':'f5','guardrail_id':'g'}
 for value in (2, 64):
  request={**base,'concurrency':value,'concurrency_group':group}
  headers={**h,'Idempotency-Key':f'concurrency-group-{value}'}
  response=c.post(ROOT+'/targets/g/runs',json=request,headers=headers)
  assert response.status_code==200,response.text
  run=response.json()
  assert run['concurrency']==value
  assert run['concurrency_group']==group
  assert run['snapshot']['environment']['runner']['concurrency']==value
  env=k.saved[run['id']]['spec']['template']['spec']['containers'][0]['env']
  assert next(v['value'] for v in env if v['name']=='EVAL_CONCURRENCY')==str(value)
  assert c.post(ROOT+'/targets/g/runs',json=request,headers=headers).json()['id']==run['id']
 assert k.created==2
 assert c.get(ROOT+'/targets/g/config',headers=h).json()['concurrency']==expected_default
 assert c.post(ROOT+'/targets/g/runs',json={**base,'concurrency':0},headers=h).status_code==422


def test_comparison_members_share_uploaded_cases_after_first_job_cleanup(setup, monkeypatch, tmp_path):
 from evaluation_backend.upload_api import cleanup_run
 from evaluation_backend.uploads import get_upload_store
 c,k,h,service,body=prepare(setup,monkeypatch)
 monkeypatch.setenv('EVAL_UPLOAD_DIR', str(tmp_path))
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h)
 assert saved.status_code==200,saved.text
 revision=saved.json()['revision']
 upload=c.post(f'{ROOT}/uploads?dataset_id=first&dataset_version=1&target_id=g&config_revision={revision}',
               content=b'input,label,strategy\nshared case,1,manual',
               headers={**h,'X-Upload-Filename':'cases.csv'})
 assert upload.status_code==200,upload.text
 upload_id=upload.json()['id']
 refs=copy.deepcopy(body['config']['datasets']);refs[0]['upload_id']=upload_id
 group='4bdd471a-a54c-460f-a946-d39d09897360'
 base=dict(config_revision=revision,datasets=refs,provider='f5',guardrail_id='g')
 for concurrency in (1, 8):
  request={**base,'concurrency_group':group,'concurrency':concurrency}
  headers={**h,'Idempotency-Key':f'group-upload-member-{concurrency}'}
  response=c.post(ROOT+'/targets/g/runs',json=request,headers=headers)
  assert response.status_code==200,response.text
  run=response.json()
  payload=c.get(ROOT+'/run-input/'+run['experiment_id'],headers=h).json()
  assert [d['rows'][0]['input'] for d in payload['datasets']]==['shared case','hello']
  cleanup_run(service,'p',run['experiment_id'])
  get_upload_store().reconcile(lambda _:True)
  assert c.post(ROOT+'/targets/g/runs',json=request,headers=headers).json()['id']==run['id']
 other=c.post(ROOT+'/targets/g/runs',json=base,headers={**h,'Idempotency-Key':'single-upload-other-1'})
 assert other.status_code==422 and 'another comparison group' in other.text
 assert k.created==2


def test_comparison_starts_jobs_without_predecessors(setup, monkeypatch):
 from evaluation_backend.runs import WAIT_FOR
 c,k,h,service,body=prepare(setup,monkeypatch)
 saved=c.post(ROOT+'/targets/g/configuration',json=body,headers=h).json()
 url=ROOT+'/targets/g/run-comparison'
 registry_url=ROOT+'/admin/guardrail-registry'
 registry=c.get(registry_url,headers=h).json()
 registry['registry']['providers'].append({
  'id':'second-guard','name':'Second Guard','instances':[{'id':'other','address':'https://other.test/scans'}],
 })
 assert c.put(registry_url,json=registry,headers=h).status_code==200
 endpoints=[{'provider':'f5','guardrail_id':'g'}, {'provider':'second-guard','guardrail_id':'other'}, {'provider':'f5','guardrail_id':'g'}]
 value=dict(config_revision=saved['revision'],levels=[64,1,4],
            group_id='4bdd471a-a54c-460f-a946-d39d09897360',endpoints=endpoints)
 assert c.post(url,json={**value,'endpoints':[endpoints[0],endpoints[1],{**endpoints[2],'guardrail_id':'missing'}]},headers=h).status_code==422
 assert k.created==0
 response=c.post(url,json=value,headers=h)
 assert response.status_code==200,response.text
 runs=response.json()['items']
 assert [r['group_index'] for r in runs]==[0,1,2]
 jobs=[k.saved[r['id']] for r in runs]
 connections=[service.storage.get('run-contexts','p',r['experiment_id'])['guardrail_connection'] for r in runs]
 assert [(connection['provider'],connection['id']) for connection in connections]==[('f5','g'),('second-guard','other'),('f5','g')]
 assert c.post(url,json={**value,'endpoints':[endpoints[0],endpoints[0],endpoints[2]]},headers=h).status_code==409
 assert c.post(url,json={**value,'levels':[1,64,4]},headers=h).status_code==409
 assert c.post(url,json=value,headers=h).status_code==200 and k.created==3

 assert all(not job['spec'].get('suspend') for job in jobs)
 assert all(WAIT_FOR not in job['metadata']['annotations'] for job in jobs)

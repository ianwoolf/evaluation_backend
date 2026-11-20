import pytest
from evaluation_backend.guardrail_metrics import aggregate_results
from evaluation_backend.guardrail_report import guardrail_report
from evaluation_backend.storage.service import EvaluationStorageService
from memory_storage import Memory

def req(id,b,outcome=None,latency=10,error=False):
 return dict(request_id=id,binding_ids=[b],strategy='s',status='ERROR' if error else 'COMPLETED',sent=True,guardrail_latency_ms=latency,classifications=[] if error else [dict(binding_id=b,outcome=outcome)])

def test_micro_average_and_latency_include_failures_not_duplicates():
 bindings=[dict(binding_id='a',dataset_id='a',dataset_version=1,sample_count=1),dict(binding_id='b',dataset_id='b',dataset_version=1,sample_count=11)]
 rows=[req('tp','a','TP',10)]+[req(str(i),'b','FN',20) for i in range(9)]+[req('error','b',latency=100,error=True)]
 result=aggregate_results(rows+[rows[0]],bindings)
 assert result['precision']==1 and result['recall']==pytest.approx(.1)
 assert result['accuracy']==pytest.approx(.1) and result['f1']==pytest.approx(2/11)
 assert result['processed']==11 and result['errors']==1 and result['pending']==1
 assert result['latency']['count']==11 and result['latency']['p95_ms']==100
 assert result['latency']['average_ms']==pytest.approx(290/11)
 empty=aggregate_results([],bindings)
 assert empty['f1'] is None and empty['accuracy'] is None and empty['latency']['average_ms'] is None


def test_dataset_error_rate_uses_complete_state_types_and_old_reports_stay_unknown():
 bindings=[dict(binding_id='a',dataset_id='a',dataset_version=1,sample_count=4)]
 requests=[{**req('ok','a','TN'), 'state_type':'ok'},
           {**req('limited','a',error=True), 'state_type':'rate-limit', 'http_status':429},
           {**req('timeout','a',error=True), 'state_type':'timeout'},
           {**req('server','a',error=True), 'state_type':'error', 'http_status':500}]
 result=aggregate_results(requests,bindings)
 assert result['state_counts']=={'ok':1,'rate-limit':1,'timeout':1,'error':1}
 assert result['error_rate']==.75
 assert result['fpr']==0 and result['fnr'] is None
 assert aggregate_results(requests[:-1],bindings)['error_rate'] is None
 assert aggregate_results([*requests[:-1],req('legacy','a',error=True)],bindings)['error_rate'] is None


def test_dataset_label_distribution_and_fpr_fnr_from_run_owned_samples():
 binding=dict(binding_id='a',dataset_id='a',dataset_version=1,sample_count=4,
              label_mapping={'1':'unsafe','0':'safe'})
 records=[('1','TP','ok'),('0','TN','ok'),('0','FP','ok'),('1',None,'rate-limit')]
 samples=[{'reference':{'label':label},'guardrail_result':{
     **req(str(index),'a',outcome,error=outcome is None),'state_type':state}}
     for index,(label,outcome,state) in enumerate(records)]
 context={'guardrail_bindings':[binding],'datasets':[{'dataset_id':'a','name':'A'}]}
 result=guardrail_report(context,samples)['dataset_results'][0]
 assert result['label_distribution']=={'positive':2,'negative':2,'total':4}
 assert result['error_rate']==.25
 assert result['fpr']==.5 and result['fnr']==0
 samples[0]['reference']={}
 samples[1]['guardrail_result'].pop('state_type')
 legacy=guardrail_report(context,samples)['dataset_results'][0]
 assert legacy['label_distribution'] is None and legacy['error_rate'] is None
 assert legacy['fpr']==.5 and legacy['fnr']==0

def test_report_identity_is_run_owned_and_dataset_results_are_independent():
 service=EvaluationStorageService(Memory())
 bindings=[dict(binding_id=i,dataset_id=i,dataset_version=1,sample_count=1,aspect_name='Safety') for i in ('a','b')]
 service.storage.put('run-contexts','p','exp',dict(id='exp',guardrail_bindings=bindings,target={'kind':'GUARDRAIL','provider':'f5','id':'evaluation-config'},guardrail_connection={'provider':'f5','id':'gr','name':'Original Guardrail','registry_revision':'old','token_env':'DO_NOT_EXPOSE'},datasets=[{'dataset_id':'a','name':'Dataset A'},{'dataset_id':'b','name':'Dataset B'}]))
 for i,outcome in [('a','TP'),('b','FN')]:service.storage.put('samples','p',i,{'evaluation_id':'exp','guardrail_result':req(i,i,outcome)})
 result=guardrail_report(service.storage.get('run-contexts','p','exp'), service.report_samples('p','exp'))
 assert result['guardrail']['name']=='Original Guardrail' and 'token_env' not in result['guardrail']
 assert result['overall']['recall']==.5
 assert [d['recall'] for d in result['dataset_results']]==[1,0]
 assert [d['name'] for d in result['dataset_results']]==['Dataset A','Dataset B']


def test_comparison_list_is_scoped_to_configuration_and_version():
 from evaluation_backend.guardrail_runs import reports
 service=EvaluationStorageService(Memory())
 for ident,config,version in [('one','cfg',1),('two','cfg',1),('new','cfg',2),('other','another',1)]:
  service.storage.put('run-contexts','p',ident,{'id':ident,'execution_mode':'guardrail','evaluation_config_id':config,'evaluation_config_version':version,'job_name':'eval-'+ident,'created_at':'2026-11-04T00:00:00Z'})
  service.storage.put('experiments','p',ident,{'status':'COMPLETED'})
 rows=reports('p',service,config_id='cfg',config_version=1)['items']
 assert {r['id'] for r in rows}=={'one','two'}
 assert all(r['report_id'].startswith('eval-') and r['created_at'] for r in rows)
 assert reports('another-project',service,config_id='cfg',config_version=1)['items']==[]

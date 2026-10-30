import copy
from datetime import datetime, timezone
import pytest
from test_runs import setup
from test_uploads import SCHEMA
from evaluation_backend.storage.service import get_service
from evaluation_backend.storage.experiment import StoredExperiment
from evaluation_backend.experiment import Settings, Scorer, Score

ROOT = '/api/v1/evaluations'

def prepare(setup, source='published'):
    client, cluster, _, headers = setup
    service = client.app.dependency_overrides[get_service]()
    for ident, count, expected in [('d', 101, True), ('second', 2, False)]:
        content = {**copy.deepcopy(SCHEMA), 'name': ident, 'rows': [
            {'messages': [{'role':'user','content':'hello'}], 'has_issue': expected} for _ in range(count)]}
        service.storage.put('datasets', 'p', ident, {'id': ident, 'versions':[content]})
    config = dict(dataset_id='d', dataset_version=1, environment_id='e', environment_version=1,
                  scorer_ids=['mock-score'], scoring_mode='mock', data_source=source, datasets=[{'dataset_id':'d','dataset_version':1},{'dataset_id':'second','dataset_version':1}])
    saved = client.post(ROOT+'/targets/g/config', headers=headers, json=config)
    assert saved.status_code == 200, saved.text
    return client, cluster, headers, service, {'config_revision':saved.json()['revision'], 'datasets':[
        {'dataset_id':'d','dataset_version':1}, {'dataset_id':'second','dataset_version':1}]}


def test_multi_dataset_execution_and_report_aggregates_all_pages(setup, monkeypatch):
    c,k,h,service,body = prepare(setup)
    result = c.post(ROOT+'/targets/g/runs',headers=h,json=body)
    assert result.status_code == 200, result.text
    run = result.json(); exp = run['experiment_id']
    assert run['sample_count'] == 103 and len(run['datasets']) == 2
    assert c.post(ROOT+'/targets/g/runs',headers=h,json=body).json()['id'] == run['id']
    assert k.created == 1
    changed = copy.deepcopy(body); changed['datasets'].reverse()
    assert c.post(ROOT+'/targets/g/runs',headers=h,json=changed).status_code == 409
    monkeypatch.setenv('EVAL_RUN_INPUT_ID', exp)
    monkeypatch.setenv('EVAL_MODEL_ID', 'none')
    snapshots = []
    class Client:
        def run_input(self, ident):
            response = c.get(ROOT+'/run-input/'+ident,headers=h)
            assert response.status_code == 200
            return response.json()
        def sample(self, ident, value):
            response = c.post(ROOT+'/experiments/'+ident+'/samples',headers=h,json=value)
            assert response.status_code == 200, response.text
        def experiment(self, ident, value):
            snapshots.append(copy.deepcopy(value))
            response = c.post(ROOT+'/experiments/'+ident,headers=h,json=value)
            assert response.status_code == 200, response.text
    class Library:
        def get_scorers(self, dataset, ids):
            return [Scorer('mock-score','1',.8,lambda **kw: Score(float(kw['reference']['has_issue']), 'test'))]
    settings = Settings('d',exp,['mock-score'],datetime.now(timezone.utc))
    summary = StoredExperiment(Client()).run(settings, Library(), None)
    assert summary['status'] == 'COMPLETED' and summary['items'] == 103
    assert all(s['status']=='RUNNING' for s in snapshots[:-1])
    assert snapshots[-1]['dataset_results'][1]['items'] == 2
    report = c.get(ROOT+'/reports/'+exp+'?limit=1',headers=h).json()
    assert report['total'] == 103 and len(report['results']) == 1
    first, second = report['datasets']
    assert first['completed'] == 101 and first['pass_rate'] == 1
    assert second['completed'] == 2 and second['pass_rate'] == 0
    assert first['scores'][0]['average'] == 1 and second['scores'][0]['average'] == 0
    # Context stays immutable despite summary writes (important for ES field merges).
    assert service.storage.get('run-contexts','p',exp)['datasets'][1]['dataset_id'] == 'second'
    rows = c.get(ROOT+'/experiments/'+exp+'/samples?offset=100',headers=h).json()['items']
    assert len({r['id'] for r in rows}) == 3
    assert all(r['dataset_version']==1 for r in rows)
    assert c.get(ROOT+'/reports/'+exp,headers={**h,'X-Project-ID':'other'}).status_code == 404


@pytest.mark.parametrize('selection,code', [
    ([],422), ([{'dataset_id':'d','dataset_version':1}]*2,422),
    ([{'dataset_id':'missing','dataset_version':1}],422),
    ([{'dataset_id':'second','dataset_version':99}],422),
])
def test_invalid_multi_dataset_selection_never_launches(setup,selection,code):
    c,k,h,_,body = prepare(setup)
    body['datasets'] = selection
    assert c.post(ROOT+'/targets/g/runs',headers=h,json=body).status_code == code
    assert k.created == 0


def test_multi_upload_claims_inputs_and_cleans_all_files(setup, monkeypatch, tmp_path):
    from evaluation_backend.uploads import get_upload_store
    from evaluation_backend.upload_api import cleanup_run
    monkeypatch.setenv('EVAL_UPLOAD_DIR',str(tmp_path))
    c,k,h,service,body = prepare(setup,'upload')
    store = get_upload_store()
    assert c.post(ROOT+'/targets/g/runs',headers=h,json=body).status_code == 422
    for selection in body['datasets']:
        schema = service.dataset_version('p',selection['dataset_id'],1)
        selection['upload_id'] = store.create('p',selection['dataset_id'],1,schema,schema['rows'][:1])['id']
    response = c.post(ROOT+'/targets/g/runs',headers=h,json=body)
    assert response.status_code == 200, response.text
    run = response.json(); exp = run['experiment_id']
    assert run['sample_count'] == 2
    assert 'upload_id' not in str(run['datasets'])
    data = c.get(ROOT+'/run-input/'+exp,headers=h).json()
    assert len(data['datasets']) == 2 and all(len(d['rows'])==1 for d in data['datasets'])
    cleanup_run(service,'p',exp)
    assert c.post(ROOT+'/targets/g/runs',headers=h,json=body).json()['id'] == run['id']
    assert c.get(ROOT+'/run-input/'+exp,headers=h).status_code == 410


def test_dataset_failure_keeps_other_dataset_results(monkeypatch):
    monkeypatch.setenv('EVAL_RUN_INPUT_ID','exp-multi')
    monkeypatch.setenv('EVAL_MODEL_ID','none')
    saved=[]; summaries=[]
    class Client:
        def run_input(self, _):
            return {'datasets':[{**copy.deepcopy(SCHEMA),'dataset_id':ident,'rows':[{'messages':[],'has_issue':True}]} for ident in ('bad','good')]}
        def sample(self, _, value):saved.append(copy.deepcopy(value))
        def experiment(self, _, value):summaries.append(copy.deepcopy(value))
    def fail(**_):raise ValueError('private error')
    class Library:
        def get_scorers(self,dataset,ids):
            return [Scorer('score','1',.8,fail if dataset.id=='bad' else lambda **_:Score(1,'ok'))]
    result=StoredExperiment(Client()).run(Settings('bad','exp-multi',['score'],datetime.now(timezone.utc)),Library(),None)
    assert result['status']=='FAILED' and result['items']==2 and result['errors']==1
    assert [d['status'] for d in result['dataset_results']]==['FAILED','COMPLETED']
    assert [s['dataset_id'] for s in saved]==['bad','good']
    assert len({s['id'] for s in saved})==2
    assert 'private error' not in str(saved)


def test_configured_subset_and_legacy_scope(setup):
    c,k,h,service,body=prepare(setup)
    config=c.get(ROOT+'/targets/g/config',headers=h).json()
    assert config['datasets']==body['datasets']
    # Existing but unconfigured resources are not eligible.
    service.storage.put('datasets','p','outside',{'versions':[copy.deepcopy(SCHEMA)]})
    outside={**body,'datasets':[{'dataset_id':'outside','dataset_version':1}]}
    assert c.post(ROOT+'/targets/g/runs',headers=h,json=outside).status_code==422
    assert k.created==0
    body['datasets']=body['datasets'][1:]
    response=c.post(ROOT+'/targets/g/runs',headers=h,json=body)
    assert response.status_code==200,response.text
    assert response.json()['dataset_id']=='second'
    # Old stored configs expose exactly their original selection.
    service.storage.put('target-configs','p','legacy',{'dataset_id':'d','dataset_version':1})
    assert c.get(ROOT+'/targets/legacy/config',headers=h).json()['datasets']==[{'dataset_id':'d','dataset_version':1}]


@pytest.mark.parametrize('datasets', [[],[{'dataset_id':'d','dataset_version':1}]*2,
    [{'dataset_id':'d','dataset_version':1,'upload_id':'upload-'+'a'*32}]])
def test_invalid_config_dataset_list_rejected(setup,datasets):
    c,k,h,_,_=prepare(setup)
    current=c.get(ROOT+'/targets/g/config',headers=h).json()
    value={key:current[key] for key in ('revision','dataset_id','dataset_version','environment_id','environment_version','scorer_ids','scoring_mode','data_source')}
    response=c.post(ROOT+'/targets/g/config',headers=h,json={**value,'datasets':datasets})
    assert response.status_code==422,response.text
    assert k.created==0

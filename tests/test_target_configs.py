from test_runs import setup


def test_config_persistence_trigger_optional_name_and_pinning(setup):
    c,k,b,h=setup
    path='/api/v1/evaluations/targets/g/config'
    config={'dataset_id':'d','dataset_version':1,'environment_id':'e','environment_version':1,'scorer_ids':['mock-score'],'scoring_mode':'mock','data_source':'published'}
    assert c.get(path,headers=h).status_code==404
    saved=c.post(path,json=config,headers=h)
    assert saved.status_code==200,saved.text
    value=c.get(path,headers=h).json()
    assert value['dataset_id']=='d'
    run=c.post('/api/v1/evaluations/targets/g/runs',json={'config_revision':value['revision'],'name':'   '},headers=h)
    assert run.status_code==200,run.text
    assert run.json()['name'].startswith('Guardrail · ')
    assert run.json()['snapshot']['environment']['cpuMillicores']==750
    retry=c.post('/api/v1/evaluations/targets/g/runs',json={'config_revision':value['revision'],'name':'   '},headers=h)
    assert retry.json()['name']==run.json()['name'] and k.created==1
    assert c.post(path,json=config,headers=h).status_code==409
    assert c.get(path,headers={**h,'X-Project-ID':'other'}).status_code==404


def test_upload_config_never_stores_file_and_requires_fresh_upload(setup):
    c,k,b,h=setup
    config={'dataset_id':'d','dataset_version':1,'environment_id':'e','environment_version':1,'scorer_ids':['mock-score'],'scoring_mode':'mock','data_source':'upload'}
    result=c.post('/api/v1/evaluations/targets/g/config',json=config,headers=h)
    assert result.status_code==200,result.text
    assert c.post('/api/v1/evaluations/targets/g/runs',json={'config_revision':result.json()['revision']},headers=h).status_code==422
    assert k.created==0
    config['upload_id']='upload-'+'a'*32
    assert c.post('/api/v1/evaluations/targets/g/config',json=config,headers=h).status_code==422


def test_stale_trigger_and_invalid_config_do_not_launch(setup):
    c,k,b,h=setup
    path='/api/v1/evaluations/targets/g/config'
    config={'dataset_id':'d','dataset_version':1,'environment_id':'e','environment_version':1,'scorer_ids':['mock-score'],'scoring_mode':'mock','data_source':'published'}
    saved=c.post(path,json=config,headers=h).json()
    config['revision']=saved['revision'];c.post(path,json=config,headers=h)
    assert c.post('/api/v1/evaluations/targets/g/runs',json={'config_revision':saved['revision']},headers=h).status_code==409
    config['dataset_id']='missing'
    assert c.post(path,json=config,headers=h).status_code==404
    assert k.created==0


def test_none_config_uses_pinned_environment_model_and_rejects_override(setup):
    c,k,b,h=setup
    env=c.get('/api/v1/evaluations/environments/e',headers=h).json()
    config={'name':'No model','modelId':'none','guardrailId':'g','cpuMillicores':750,'memoryMiB':768}
    saved=c.post('/api/v1/evaluations/environments/e',json={'revision':env['revision'],'config':config},headers=h)
    assert saved.status_code==200,saved.text
    selection={'dataset_id':'d','dataset_version':1,'environment_id':'e','environment_version':2,'scorer_ids':['mock-score']}
    saved=c.post('/api/v1/evaluations/targets/g/config',json=selection,headers=h)
    assert saved.status_code==200,saved.text
    assert saved.json()['model_id']=='none'
    # An explicit request cannot override the model in the pinned environment.
    wrong={**b,'environment_version':2,'model_id':'glm-dev'}
    assert c.post('/api/v1/evaluations/runs',json=wrong,headers=h).status_code==422
    run=c.post('/api/v1/evaluations/targets/g/runs',json={'config_revision':saved.json()['revision']},headers=h)
    assert run.status_code==200,run.text
    assert run.json()['model_id']=='none'
    assert run.json()['snapshot']['environment']['modelId']=='none'


def test_sample_trace_retains_zero_model_latency_without_model_step(setup):
    c,k,b,h=setup
    sample={'id':'sample-no-model','input':{'messages':[]},'output':{'messages':[]},
            'reference':{},'status':'PASS','latencyMs':10,'modelLatencyMs':0,
            'scores':[],'trace':[{'kind':'SCORER','name':'test','durationMs':10}]}
    assert c.post('/api/v1/evaluations/experiments/no-model/samples',json=sample,headers=h).status_code==200
    traces=c.get('/api/v1/evaluations/traces?evaluation_id=no-model',headers=h).json()['items']
    assert len(traces)==1
    assert traces[0]['data']['modelLatencyMs']==0
    assert [step['kind'] for step in traces[0]['data']['trace']]==['SCORER']


def test_new_stored_resources_can_save_and_trigger_without_bindings(setup):
    import copy
    from evaluation_backend.storage.service import get_service
    from test_uploads import SCHEMA
    c,k,b,h=setup
    service=c.app.dependency_overrides[get_service]()
    version={**copy.deepcopy(SCHEMA), 'version':2}
    service.storage.put('datasets','p','new-dataset',{'id':'new-dataset','versions':[version]})
    service.storage.put('environments','p','new-environment',{'id':'new-environment','versions':[
        {'name':'New environment','version':3,'cpuMillicores':500,'memoryMiB':512,
         'modelId':'custom-model','guardrailId':'g'}]})
    config={'dataset_id':'new-dataset','dataset_version':2,'environment_id':'new-environment',
            'environment_version':3,'scorer_ids':['mock-score'],'scoring_mode':'mock'}
    response=c.post('/api/v1/evaluations/targets/g/config',json=config,headers=h)
    assert response.status_code==200,response.text
    assert response.json()['model_id']=='custom-model'
    run=c.post('/api/v1/evaluations/targets/g/runs',
               json={'config_revision':response.json()['revision']},headers=h)
    assert run.status_code==200,run.text
    assert run.json()['model_id']=='custom-model'
    assert k.created==1


def test_invalid_resources_and_scorers_never_save_or_launch(setup):
    c,k,b,h=setup
    config={'dataset_id':'d','dataset_version':1,'environment_id':'e','environment_version':1,
            'scorer_ids':['mock-score'],'scoring_mode':'mock'}
    for patch,status in [({'dataset_id':'missing'},404),({'dataset_version':99},404),
                         ({'environment_id':'missing'},404),({'environment_version':99},404),
                         ({'scorer_ids':['missing'],'scoring_mode':'installed'},422)]:
        response=c.post('/api/v1/evaluations/targets/g/config',json={**config,**patch},headers=h)
        assert response.status_code==status,response.text
    assert c.post('/api/v1/evaluations/targets/missing/config',json=config,headers=h).status_code==404
    for patch,status in [({'dataset_id':'missing'},404),({'dataset_version':99},404),
                         ({'environment_id':'missing'},404),({'environment_version':99},404),
                         ({'target_id':'missing'},404)]:
        response=c.post('/api/v1/evaluations/runs',json={**b,**patch},headers=h)
        assert response.status_code==status,response.text
    assert k.created==0


def test_runtime_configuration_has_no_bindings_dependency(setup):
    c,k,b,h=setup
    response=c.get('/api/v1/evaluations/config',headers=h)
    assert response.status_code==200
    assert 'bindings' not in response.json()

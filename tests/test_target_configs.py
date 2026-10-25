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
    assert c.post(path,json=config,headers=h).status_code==422
    assert k.created==0

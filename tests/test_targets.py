import copy
import json
import httpx
from test_runs import setup
from evaluation_backend.storage.elastic import ElasticsearchStorage


def test_database_catalog_project_isolation_and_revision_updates(setup, monkeypatch):
    c,k,b,h=setup
    monkeypatch.setenv('EVAL_TARGETS', '[{"id":"ignored-config-target"}]')
    path='/api/v1/evaluations/targets'
    assert c.get(path).status_code==401
    assert [v['id'] for v in c.get(path,headers=h).json()['items']]==['g']
    assert c.get(path,headers={**h,'X-Project-ID':'other'}).json()['items']==[]
    target={'id':'new','name':'New Guardrail','provider':'f5','version':1,'definition':'Test policy'}
    saved=c.post(path+'/new',json=target,headers=h)
    assert saved.status_code==200,saved.text
    assert c.post(path+'/new',json=target,headers=h).status_code==409
    assert c.get(path+'/new',headers={**h,'X-Project-ID':'other'}).status_code==404
    revision=saved.json()['revision']
    update={**target,'name':'Updated','version':2,'revision':revision}
    assert c.post(path+'/new',json={**update,'version':1},headers=h).status_code==409
    assert c.post(path+'/wrong',json=target,headers=h).status_code==422
    assert c.post(path+'/new',json=update,headers=h).status_code==200
    assert c.post(path+'/new',json=update,headers=h).status_code==409
    assert c.get(path+'/new',headers=h).json()['name']=='Updated'
    assert c.get(path+'?limit=0',headers=h).status_code==422


def test_runs_pin_database_target_even_after_catalog_updates(setup):
    c,k,b,h=setup
    run=c.post('/api/v1/evaluations/runs',json=b,headers=h).json()
    target=c.get('/api/v1/evaluations/targets/g',headers=h).json()
    response=c.post('/api/v1/evaluations/targets/g',json={**target,'name':'Edited','version':3},headers=h)
    assert response.status_code==200,response.text
    original=c.get('/api/v1/evaluations/runs/'+run['id'],headers=h).json()
    assert original['snapshot']['target']['name']=='Guardrail'
    new=c.post('/api/v1/evaluations/runs',json=b,headers={**h,'Idempotency-Key':'new-target-version-123456'})
    assert new.status_code==200,new.text
    assert new.json()['snapshot']['target']['name']=='Edited'
    assert new.json()['snapshot']['target']['version']==3


def test_targets_reuse_records_index_with_distinct_resource_type():
    docs={};requests=[]
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/_search'):
            body=json.loads(request.content)
            assert body['query']['bool']['filter']==[{'term':{'project_id':'p'}},
                {'term':{'record_type':'target'}},{'term':{'state':'head'}}]
            hits=[{'_source':v,'_seq_no':0,'_primary_term':1} for v in docs.values()]
            return httpx.Response(200,json={'hits':{'hits':hits,'total':{'value':len(hits)}}})
        if request.method=='PUT':
            docs[str(request.url.path)]=json.loads(request.content)
            return httpx.Response(200,json={'_seq_no':0,'_primary_term':1})
        return httpx.Response(200,json={'_source':docs[str(request.url.path)],'_seq_no':0,'_primary_term':1})
    store=ElasticsearchStorage('http://es.test',client=httpx.Client(transport=httpx.MockTransport(handle)))
    target={'id':'g','name':'Guardrail','kind':'GUARDRAIL','provider':'f5','version':2,'definition':'Test'}
    store.put('targets','p','g',target,create=True)
    source=next(iter(docs.values()))
    assert source['record_type']=='target' and source['spec']==target
    assert store.record_key('targets','p','g')!=store.record_key('target-configs','p','g')
    assert store.get('targets','p','g')['name']=='Guardrail'
    assert store.search('targets','p')['items'][0]['provider']=='f5'
    assert all(r.url.path.startswith('/evaluation-records-v2/') for r in requests)

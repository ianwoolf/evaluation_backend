import json, struct
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from fastapi import HTTPException
from evaluation_backend.docker_runtime import DockerRuntime, OWNER, RECORD, NETWORK
from evaluation_backend.k8s_job import build_job
from evaluation_backend.deployment_config import split_models


def workload():
    job=build_job('d','none',['mock-score'],mock=True,namespace='docker',experiment_id='exp-test')
    job['metadata']['labels']['project']='project-hash'
    job['metadata']['annotations']={'evaluation.tasklattice.io/project':'individual','evaluation.tasklattice.io/record':'{}'}
    return job


def test_docker_create_limits_secrets_and_no_kubernetes(monkeypatch):
    monkeypatch.setenv('EVAL_DOCKER_NETWORK','test-network')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN','never-in-worker')
    monkeypatch.setenv('EVAL_MODEL_TOKEN_ABCDEF123456','never-provider-token')
    monkeypatch.setenv('EVAL_MODEL_RESOLVER_TOKEN_FILE','/run/secrets/resolver_token')
    monkeypatch.setenv('EVAL_MODEL_RESOLVER_TOKEN','file-backed-value')
    monkeypatch.setenv('HOSTNAME','api-container')
    saved={}
    def handle(request):
        path=request.url.path
        assert path.startswith('/v1.45/')
        if path.endswith('/api-container/json'):
            return httpx.Response(200,json={'Mounts':[{'Destination':'/run/secrets/resolver_token','Source':'/host/private/resolver_token'}]})
        if path.endswith('/containers/create'):
            saved.update(json.loads(request.content));return httpx.Response(201,json={'Id':'worker'})
        if path.endswith('/worker/start'):return httpx.Response(204)
        if path.endswith('/worker/json'):
            return httpx.Response(200,json={'Id':'worker','Created':'2026-01-01T00:00:00Z','Config':{'Labels':saved['Labels']},'State':{'Running':True}})
        raise AssertionError(path)
    client=httpx.Client(base_url='http://docker/v1.45',transport=httpx.MockTransport(handle))
    result=DockerRuntime(client).create(workload())
    env=dict(item.split('=',1) for item in saved['Env'])
    assert 'EVAL_TRIGGER_TOKEN' not in env and 'EVAL_MODEL_TOKEN_ABCDEF123456' not in env
    assert 'EVAL_MODEL_RESOLVER_TOKEN' not in env
    assert env['EVAL_MODEL_RESOLVER_TOKEN_FILE']=='/run/secrets/resolver_token'
    assert saved['HostConfig']['Binds']==['/host/private/resolver_token:/run/secrets/resolver_token:ro']
    assert saved['HostConfig']['NetworkMode']=='test-network'
    assert saved['HostConfig']['NanoCpus']==2_000_000_000
    assert saved['HostConfig']['Memory']==2048*1024*1024
    assert saved['HostConfig']['ReadonlyRootfs'] and saved['User']=='10001:10001'
    assert 'never-in-worker' not in json.dumps(saved)
    assert 'file-backed-value' not in json.dumps(saved)
    assert result['status']['active']==1


def test_docker_deadline_and_log_frames():
    stopped=[];record=workload()
    record={'metadata':record['metadata'],'spec':{'activeDeadlineSeconds':1}}
    container={'Id':'worker','Created':'2026-01-01T00:00:00Z','Config':{'Labels':{OWNER:'evaluation-backend',NETWORK:'evaluation-compose',RECORD:json.dumps(record)}},
               'State':{'Running':True,'StartedAt':(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat()}}
    def handle(request):
        if request.url.path.endswith('/stop'):
            stopped.append(True);container['State']={'Running':False,'Status':'exited','ExitCode':137};return httpx.Response(204)
        if request.url.path.endswith('/json'):return httpx.Response(200,json=container)
        if request.url.path.endswith('/logs'):
            payload=b'{"evaluation_id":"exp-test","items":2}\n'
            return httpx.Response(200,content=b'\x01\x00\x00\x00'+struct.pack('>I',len(payload))+payload)
        raise AssertionError(str(request.url))
    runtime=DockerRuntime(httpx.Client(base_url='http://docker/v1.45',transport=httpx.MockTransport(handle)))
    assert runtime.job(container)['status']['conditions'][0]['type']=='Failed'
    assert stopped and json.loads(runtime.logs('worker',100))['items']==2


def test_docker_does_not_inspect_unowned_containers():
    runtime=DockerRuntime(httpx.Client(base_url='http://docker/v1.45',transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'Config':{'Labels':{}}}))))
    with pytest.raises(HTTPException) as caught:runtime.inspect('foreign')
    assert caught.value.status_code==404


def test_model_metadata_has_no_tokens_and_credentials_are_separate():
    public,tokens=split_models({'glm-dev':{'address':'https://model.example/v1','model':'glm','token':'provider-secret'}})
    assert 'provider-secret' not in json.dumps(public)
    assert tokens[public['glm-dev']['token_env']]=='provider-secret'

import pytest
from fastapi.testclient import TestClient
from evaluation_backend.api import create_app
from evaluation_backend.storage.service import get_service, EvaluationStorageService
from memory_storage import Memory

CONFIG = {'name':'Isolated','modelId':'glm-dev','guardrailId':'guardrail-privacy','cpuMillicores':750,'memoryMiB':768}


def test_environment_persistence_patch_versions_conflict_and_project_boundary(monkeypatch):
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN','secret')
    service=EvaluationStorageService(Memory()); app=create_app()
    app.dependency_overrides[get_service]=lambda: service
    h={'Authorization':'Bearer secret','X-Project-ID':'p'}; path='/api/v1/evaluations/environments/e'
    with TestClient(app) as c:
        assert c.post(path,json={'config':CONFIG},headers=h).status_code==200
        initial=c.get(path,headers=h).json()
        assert initial['versions'][0]['cpuMillicores']==750
        patched=c.patch(path,json={'revision':initial['revision'],'cpuMillicores':500},headers=h)
        assert patched.status_code==200,patched.text
        assert [v['cpuMillicores'] for v in patched.json()['versions']]==[750,500]
        assert c.patch(path,json={'revision':initial['revision'],'memoryMiB':1024},headers=h).status_code==409
        assert c.get(path+'/versions/1',headers=h).json()['cpuMillicores']==750
        assert c.get(path,headers={**h,'X-Project-ID':'other'}).status_code==404
        for v in [0,32001,0.5,True,'500m']:
            assert c.patch(path,json={'revision':patched.json()['revision'],'cpuMillicores':v},headers=h).status_code==422
    with TestClient(app) as c:
        assert c.get(path,headers=h).json()['versions'][-1]['cpuMillicores']==500

import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from evaluation_backend.guardrail_registry import registry,resolve,read_file,credential

def install(tmp_path,monkeypatch,**changes):
    instance={'id':'shared','name':'Guard','address':'https://guard.test/scans','token_env':'GUARD_TOKEN',**changes}
    doc={'version':1,'providers':[{'id':'f5','name':'F5','instances':[instance]},{'id':'nemo guardrail','name':'NeMo','instances':[dict(instance,address='https://nemo.test/scans')]}]}
    path=tmp_path/'registry.json';path.write_text(json.dumps(doc));monkeypatch.setenv('EVAL_GUARDRAIL_REGISTRY',str(path));read_file.cache_clear()
    return path

def test_provider_scope_public_catalog_and_secret_file(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch)
    assert resolve({'id':'config','provider':'f5','guardrail_id':'shared'})['address']=='https://guard.test/scans'
    assert resolve({'id':'config','provider':'nemo guardrail','guardrail_id':'shared'})['address']=='https://nemo.test/scans'
    from evaluation_backend.guardrail_registry import catalog
    value=catalog(service=None);assert 'token_env' not in json.dumps(value)
    assert len(catalog('f5', service=None)['providers'])==1
    secret=tmp_path/'token';secret.write_text('CANARY_TOKEN\n');monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN_FILE',str(secret))
    assert credential()=='CANARY_TOKEN'
    assert 'CANARY_TOKEN' not in json.dumps(value)

def test_disabled_invalid_and_unknown_fail_closed(tmp_path,monkeypatch):
    path=install(tmp_path,monkeypatch,enabled=False)
    with pytest.raises(HTTPException):resolve({'id':'shared','provider':'f5'})
    with pytest.raises(HTTPException):resolve({'id':'shared','provider':'unknown'})
    path.write_text('{bad json');read_file.cache_clear()
    with pytest.raises(RuntimeError):registry()

def test_pinned_endpoint_survives_registry_changes(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch);monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN','CANARY_TOKEN')
    connection=resolve({'id':'shared','provider':'f5'})
    install(tmp_path,monkeypatch,address='https://changed.test/scans')
    assert connection['address']=='https://guard.test/scans'
    assert resolve({'id':'shared','provider':'f5'})['address']=='https://changed.test/scans'
    assert 'CANARY_TOKEN' not in json.dumps(connection)

def test_verify_ssl_defaults_on_and_is_pinned_in_runner_detail(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from evaluation_backend import guardrail_runs

    target = {'id': 'shared', 'provider': 'f5'}
    install(tmp_path, monkeypatch)
    assert resolve(target)['verify_ssl'] is True
    install(tmp_path, monkeypatch, verify_ssl=False, token_env=None)
    connection = resolve(target)
    install(tmp_path, monkeypatch)
    monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN', 'test-token')
    context = {'execution_mode': 'guardrail', 'guardrail_connection': connection}
    service = SimpleNamespace(storage=SimpleNamespace(get=lambda *_: context))
    monkeypatch.setattr(guardrail_runs, 'verify_worker_token', lambda _: {'project': 'p', 'evaluation': 'run'})
    assert guardrail_runs.detail('run', 'Bearer fixture', 'p', service)['verify_ssl'] is False
    del connection['verify_ssl']
    assert guardrail_runs.detail('run', 'Bearer fixture', 'p', service)['verify_ssl'] is True

@pytest.mark.parametrize('address',['file:///etc/passwd','https://user:pass@guard.test/scans','https://guard.test/scans?token=bad'])
def test_reject_invalid_endpoint(tmp_path,monkeypatch,address):
    install(tmp_path,monkeypatch,address=address)
    with pytest.raises(RuntimeError):registry()


def test_admin_registry_persists_per_project_and_is_used_by_trigger_resolution(tmp_path, monkeypatch):
    from evaluation_backend.api import create_app
    from evaluation_backend.storage.service import EvaluationStorageService, get_service
    from memory_storage import Memory

    install(tmp_path, monkeypatch)
    monkeypatch.setenv('EVAL_AUTH_MODE', 'token')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN', 'trigger-test')
    monkeypatch.setenv('EVAL_ADMIN_TOKEN', 'admin-test')
    monkeypatch.setenv('EVAL_GUARDRAIL_TOKEN', 'shared-test')
    monkeypatch.setenv('GUARD_TOKEN', 'legacy-per-instance')
    store = Memory()
    service = EvaluationStorageService(store)
    app = create_app()
    app.dependency_overrides[get_service] = lambda: service
    client = TestClient(app)
    path = '/api/v1/evaluations/admin/guardrail-registry'
    admin = {'Authorization': 'Bearer admin-test', 'X-Project-ID': 'p'}
    assert client.get(path, headers={**admin, 'Authorization': 'Bearer trigger-test'}).status_code == 401
    baseline = client.get(path, headers=admin).json()
    update = json.loads(json.dumps(baseline))
    update['registry']['providers'][0]['instances'][0]['address'] = 'https://managed.test/scans'
    saved = client.put(path, headers=admin, json=update)
    assert saved.status_code == 200, saved.text
    assert client.put(path, headers=admin, json=update).status_code == 409
    target = {'id': 'shared', 'provider': 'f5'}
    assert resolve(target, service, 'p')['address'] == 'https://managed.test/scans'
    assert resolve(target, service, 'other')['address'] == 'https://guard.test/scans'
    assert credential() == 'shared-test'
    catalog = client.get('/api/v1/evaluations/guardrail-instances', headers={
        'X-Project-ID': 'p', 'Authorization': 'Bearer trigger-test',
    }).json()
    assert catalog['providers'][0]['instances'][0]['address'] == 'https://managed.test/scans'
    assert 'token_env' not in saved.text and 'shared-test' not in saved.text
    attempted = saved.json()
    attempted['registry']['providers'][0]['instances'][0]['token_env'] = 'GUARD_TOKEN'
    assert client.put(path, headers=admin, json=attempted).status_code == 422

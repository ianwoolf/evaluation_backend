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
    value=catalog();assert 'token_env' not in json.dumps(value)
    assert len(catalog('f5')['providers'])==1
    secret=tmp_path/'token';secret.write_text('CANARY_TOKEN\n');monkeypatch.setenv('GUARD_TOKEN_FILE',str(secret))
    assert credential(resolve({'id':'shared','provider':'f5'}))=='CANARY_TOKEN'
    assert 'CANARY_TOKEN' not in json.dumps(value)

def test_disabled_invalid_and_unknown_fail_closed(tmp_path,monkeypatch):
    path=install(tmp_path,monkeypatch,enabled=False)
    with pytest.raises(HTTPException):resolve({'id':'shared','provider':'f5'})
    with pytest.raises(HTTPException):resolve({'id':'shared','provider':'unknown'})
    path.write_text('{bad json');read_file.cache_clear()
    with pytest.raises(RuntimeError):registry()

def test_pinned_endpoint_survives_registry_changes(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch);monkeypatch.setenv('GUARD_TOKEN','CANARY_TOKEN')
    connection=resolve({'id':'shared','provider':'f5'})
    install(tmp_path,monkeypatch,address='https://changed.test/scans')
    assert connection['address']=='https://guard.test/scans'
    assert resolve({'id':'shared','provider':'f5'})['address']=='https://changed.test/scans'
    assert 'CANARY_TOKEN' not in json.dumps(connection)

@pytest.mark.parametrize('address',['file:///etc/passwd','https://user:pass@guard.test/scans','https://guard.test/scans?token=bad'])
def test_reject_invalid_endpoint(tmp_path,monkeypatch,address):
    install(tmp_path,monkeypatch,address=address)
    with pytest.raises(RuntimeError):registry()

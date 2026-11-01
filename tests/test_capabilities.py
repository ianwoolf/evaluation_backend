import pytest
from fastapi.testclient import TestClient
from evaluation_backend.capabilities import resolve_modes, scoring_enabled
from evaluation_backend.api import create_app

@pytest.mark.parametrize('scoring,reflection,expected', [(False,False,'mock'),(False,True,'mock'),(True,False,'mock'),(True,True,'real')])
def test_modes(scoring, reflection, expected):
    assert resolve_modes(scoring_enabled=scoring, reflection_enabled=reflection)['effective_reflection_mode'] == expected

def test_invalid_flag_rejected(monkeypatch):
    monkeypatch.setenv('EVAL_SCORING_ENABLED','perhaps')
    with pytest.raises(ValueError): scoring_enabled()

def test_capabilities_endpoint(monkeypatch):
    monkeypatch.setenv('EVAL_AUTH_MODE','none')
    monkeypatch.setenv('EVAL_SCORING_ENABLED','false')
    with TestClient(create_app()) as client:
        value = client.get('/api/v1/evaluations/capabilities').json()
    assert value['scoring_enabled'] is False
    assert value['effective_reflection_mode'] == 'mock'

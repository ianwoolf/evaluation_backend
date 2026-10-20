import json
from fastapi.testclient import TestClient
import httpx
import pytest
from evaluation_backend.llm_proxy import create_proxy, merge
from evaluation_backend.api import create_app


class Span:
    def __init__(self):
        self.updates = []
        self.ended = False
    def update(self, **kwargs): self.updates.append(kwargs)
    def end(self): self.ended = True


class Telemetry:
    def start_observation(self, **kwargs):
        self.started = kwargs
        self.span = Span()
        return self.span
    def flush(self): pass


def setup(stream_error=False):
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'address': 'https://provider.test/v4', 'model': 'glm-4.7', 'token': 'provider-secret'})
        if stream_error:
            return httpx.Response(401, json={'error': 'provider-secret'})
        chunks = [{'id': 'completion-1', 'model': 'actual-model', 'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]},
                  {'choices': [{'index': 0, 'delta': {'content': 'OK'}}]},
                  {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}]
        data = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
        return httpx.Response(200, text=data)
    telemetry = Telemetry()
    env = {'EVAL_MODEL_ID': 'chosen', 'EVAL_MODEL_RESOLVER_URL': 'http://backend.test',
           'EVAL_MODEL_RESOLVER_TOKEN': 'resolver-secret', 'EVAL_LLM_PROXY_TOKEN': 'local-secret', 'EVALUATE_ID': 'dev_1'}
    app = create_proxy(env, httpx.AsyncClient(transport=httpx.MockTransport(handle)), telemetry)
    return app, telemetry, calls


@pytest.mark.parametrize('stream', [False, True])
def test_forced_model_usage_timing_and_stream(stream):
    app, telemetry, calls = setup()
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer local-secret'},
                               json={'model': 'bypass', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': stream, 'api_key': 'injected'})
    assert response.status_code == 200
    assert ('data: [DONE]' in response.text) if stream else response.json()['choices'][0]['message']['content'] == 'OK'
    payload = json.loads(calls[1].content)
    assert payload['model'] == 'glm-4.7' and payload['stream_options']['include_usage']
    assert 'api_key' not in payload
    assert calls[0].headers['authorization'] == 'Bearer resolver-secret'
    assert calls[1].headers['authorization'] == 'Bearer provider-secret'
    final = telemetry.span.updates[-1]
    assert final['usage_details'] == {'input': 10, 'output': 2, 'total': 12}
    assert 0 <= final['metadata']['ttft_ms'] <= final['metadata']['duration_ms']
    assert final['metadata']['experiment_id'] == 'dev_1'
    assert final['metadata']['response_model'] == 'actual-model'
    assert telemetry.span.ended
    assert 'provider-secret' not in str(telemetry.started) + str(telemetry.span.updates)


def test_auth_and_sanitized_provider_error():
    app, telemetry, calls = setup(True)
    with TestClient(app) as client:
        assert client.post('/v1/chat/completions', json={}).status_code == 401
        assert not calls
        r = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer local-secret'}, json={'messages': []})
        assert r.status_code == 502 and 'provider-secret' not in r.text
        assert telemetry.span.ended


def test_model_resolver_auth_and_lookup(tmp_path, monkeypatch):
    path = tmp_path / 'models.json'
    path.write_text(json.dumps({'chosen': {'address': 'https://provider.test/v4', 'model': 'glm', 'token': 'secret'}}))
    monkeypatch.setenv('EVAL_MODEL_REGISTRY', str(path))
    monkeypatch.setenv('EVAL_MODEL_RESOLVER_TOKEN', 'resolver')
    with TestClient(create_app()) as client:
        assert client.get('/internal/models/chosen').status_code == 401
        headers = {'Authorization': 'Bearer resolver'}
        assert client.get('/internal/models/missing', headers=headers).status_code == 404
        assert client.get('/internal/models/chosen', headers=headers).json()['model'] == 'glm'


def test_tool_delta_merge():
    chunks = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'tool-1', 'function': {'name': 'lookup', 'arguments': '{'}}]}}]},
              {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '}'}}]}, 'finish_reason': 'tool_calls'}]}]
    tool = merge(chunks)['choices'][0]['message']['tool_calls'][0]
    assert tool['id'] == 'tool-1' and tool['function'] == {'name': 'lookup', 'arguments': '{}'}


def test_missing_usage_and_truncated_stream_are_not_success():
    telemetry = Telemetry()
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'address': 'https://provider.test/v4', 'model': 'glm', 'token': 'secret'})
        return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n')
    env = {'EVAL_MODEL_ID': 'chosen', 'EVAL_MODEL_RESOLVER_URL': 'http://backend.test',
           'EVAL_MODEL_RESOLVER_TOKEN': 'resolver', 'EVAL_LLM_PROXY_TOKEN': 'local'}
    app = create_proxy(env, httpx.AsyncClient(transport=httpx.MockTransport(handle)), telemetry)
    with TestClient(app) as client:
        r = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer local'}, json={'messages': []})
        assert r.status_code == 502
    final = telemetry.span.updates[-1]
    assert final['usage_details'] == {}
    assert final['metadata']['interrupted'] is True
    assert final['metadata']['usage_reported'] is False
    assert telemetry.span.ended


def test_openshell_ca_is_used_without_disabling_tls(monkeypatch):
    from evaluation_backend.llm_proxy import proxy_ssl_context
    import evaluation_backend.llm_proxy as module
    seen = []
    monkeypatch.setattr(module.ssl, 'create_default_context', lambda **kwargs: seen.append(kwargs) or 'context')
    assert proxy_ssl_context({'SSL_CERT_FILE': '/runtime/ca.pem', 'REQUESTS_CA_BUNDLE': '/fallback.pem'}) == 'context'
    assert seen[-1] == {'cafile': '/runtime/ca.pem'}
    proxy_ssl_context({'REQUESTS_CA_BUNDLE': '/fallback.pem'})
    assert seen[-1] == {'cafile': '/fallback.pem'}
    proxy_ssl_context({})
    assert seen[-1] == {'cafile': None}

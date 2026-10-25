"""Worker HTTP client. Workers receive a backend token, never Elasticsearch credentials."""
import os
from urllib.parse import quote, urlparse

import httpx


class StorageClient:
    def __init__(self, env=None, client=None):
        env = os.environ if env is None else env
        base = env.get('EVAL_STORAGE_API_URL', '').rstrip('/')
        parsed = urlparse(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('EVAL_STORAGE_API_URL is required')
        token, project = env.get('EVAL_STORAGE_TOKEN', ''), env.get('EVAL_PROJECT_ID', '')
        if not token or not project:
            raise ValueError('EVAL_STORAGE_TOKEN and EVAL_PROJECT_ID are required')
        self.base = base + '/api/v1/evaluations/'
        self.write_base = env.get('EVAL_COLLECTOR_URL', base).rstrip('/') + '/api/v1/evaluations/'
        self.client = client or httpx.Client(timeout=30, trust_env=False,
            headers={'Authorization': 'Bearer ' + token, 'X-Project-ID': project})

    def call(self, method, path, value=None):
        try:
            response = self.client.request(method, (self.base if method == 'GET' else self.write_base) + path, **({'json': value} if value is not None else {}))
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Evaluation storage service request failed') from None

    def version(self, ident, version):
        return self.call('GET', f'datasets/{quote(ident, safe="")}/versions/{version}')

    def run_input(self, ident):
        return self.call('GET', 'run-input/' + quote(ident, safe=''))

    def sample(self, ident, value):
        from evaluation_backend.content_policy import ContentPolicy
        value = ContentPolicy.from_env().sample(value)
        return self.call('POST', f'experiments/{quote(ident, safe="")}/samples', value)

    def experiment(self, ident, value):
        return self.call('POST', f'experiments/{quote(ident, safe="")}', value)

    def trace(self, value):
        from evaluation_backend.content_policy import ContentPolicy
        policy = ContentPolicy.from_env()
        value = {**value, 'data': policy.trace_data(value['data'])}
        if policy.restricted: value['name'] = 'Execution trace'
        return self.call('POST', 'traces', value)

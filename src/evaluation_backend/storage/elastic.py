"""Elasticsearch REST adapter, compatible with ES 8/9; no vendor SDK in workers."""
import hashlib
import json
import os
import re
from urllib.parse import quote, urlparse

import httpx

from . import Conflict, NotFound, StorageError

COLLECTIONS = {'datasets', 'traces', 'scores', 'experiments', 'samples', 'run-contexts', 'environments', 'target-configs'}


class ElasticsearchStorage:
    def __init__(self, url, *, prefix='evaluation', api_key='', ca_file=True, client=None):
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('EVAL_ES_URL must be an HTTP(S) URL without embedded credentials')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,60}', prefix):
            raise ValueError('Invalid ES index prefix')
        self.url, self.prefix = url.rstrip('/'), prefix
        self.client = client or httpx.Client(timeout=30, trust_env=False, verify=ca_file,
            headers={'Authorization': 'ApiKey ' + api_key} if api_key else {})

    @classmethod
    def from_env(cls):
        return cls(os.getenv('EVAL_ES_URL', 'http://127.0.0.1:9200'),
                   prefix=os.getenv('EVAL_ES_PREFIX', 'evaluation'), api_key=os.getenv('EVAL_ES_API_KEY', ''),
                   ca_file=os.getenv('EVAL_ES_CA_FILE') or True)

    def call(self, method, path, **kwargs):
        try:
            response = self.client.request(method, self.url + path, **kwargs)
        except httpx.HTTPError:
            raise StorageError('Elasticsearch is unavailable') from None
        if response.status_code == 404:
            raise NotFound('Record not found')
        if response.status_code == 409:
            raise Conflict('Record already exists or revision has changed')
        if response.is_error:
            raise StorageError('Elasticsearch request failed')
        return response.json()

    def index(self, collection):
        if collection not in COLLECTIONS:
            raise ValueError('Unknown collection')
        return f'{self.prefix}-{collection}-v1'

    @staticmethod
    def key(project, ident):
        return hashlib.sha256(json.dumps([project, ident]).encode()).hexdigest()

    def initialize(self):
        # Payload remains in _source without creating arbitrary user-defined fields.
        properties = {name: {'type': 'keyword'} for name in
                      ('project_id', 'id', 'evaluation_id', 'sample_id', 'dataset_id', 'scorer_id', 'status')}
        properties.update(created_at={'type': 'date'}, payload={'type': 'object', 'enabled': False})
        for collection in sorted(COLLECTIONS):
            try:
                self.call('PUT', '/' + self.index(collection), json={'mappings': {'dynamic': 'strict', 'properties': properties}})
            except StorageError:
                # Only ignore the specific concurrent/already-created index case.
                existing = self.call('GET', '/' + self.index(collection) + '/_mapping')
                if not existing:
                    raise

    @staticmethod
    def unpack(hit):
        return {**hit['_source']['payload'], '_revision': f"{hit['_seq_no']}:{hit['_primary_term']}"}

    def get(self, collection, project, ident):
        hit = self.call('GET', f'/{self.index(collection)}/_doc/{self.key(project, ident)}')
        if hit['_source']['project_id'] != project:
            raise NotFound('Record not found')
        return self.unpack(hit)

    def put(self, collection, project, ident, value, *, create=False, revision=None):
        payload = {k: v for k, v in value.items() if k != '_revision'}
        doc = {'project_id': project, 'id': ident, 'payload': payload}
        for name in ('evaluation_id', 'sample_id', 'dataset_id', 'scorer_id', 'status', 'created_at'):
            if name in payload:
                doc[name] = payload[name]
        params = {'refresh': 'wait_for'}
        if create:
            params['op_type'] = 'create'
        if revision is not None:
            try:
                seq, term = map(int, revision.split(':'))
            except (ValueError, AttributeError):
                raise Conflict('Invalid revision') from None
            params.update(if_seq_no=seq, if_primary_term=term)
        hit = self.call('PUT', f'/{self.index(collection)}/_doc/{self.key(project, ident)}', json=doc, params=params)
        return {**payload, '_revision': f"{hit['_seq_no']}:{hit['_primary_term']}"}

    def search(self, collection, project, *, filters=None, offset=0, limit=100):
        terms = [{'term': {'project_id': project}}]
        for name, value in (filters or {}).items():
            if name not in {'evaluation_id', 'sample_id', 'dataset_id', 'scorer_id', 'status'}:
                raise ValueError('Unsupported search filter')
            terms.append({'term': {name: value}})
        result = self.call('POST', f'/{self.index(collection)}/_search', json={
            'query': {'bool': {'filter': terms}}, 'sort': [{'id': 'asc'}], 'from': offset,
            'size': limit, 'track_total_hits': True, 'seq_no_primary_term': True})
        return {'items': [self.unpack(h) for h in result['hits']['hits']], 'total': result['hits']['total']['value']}

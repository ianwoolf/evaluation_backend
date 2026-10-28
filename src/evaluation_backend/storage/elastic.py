"""Elasticsearch REST adapter, compatible with ES 8/9; no vendor SDK in workers."""
from evaluation_backend.observability import observed
import hashlib
import json
import os
import re
import ssl
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import httpx

from . import Conflict, NotFound, StorageError

COLLECTIONS = {'datasets', 'traces', 'scores', 'experiments', 'samples', 'run-contexts', 'environments', 'target-configs', 'targets'}


class ElasticsearchStorage:
    def __init__(self, url, *, prefix='evaluation', api_key='', username='', password='', verify_ssl=False, ca_file=None, client=None,
                 records_index=None, samples_index=None, traces_index=None):
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('EVAL_ES_URL must be an HTTP(S) URL without embedded credentials')
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,60}', prefix):
            raise ValueError('Invalid ES index prefix')
        if bool(username) != bool(password):
            raise ValueError('EVAL_ES_USERNAME and EVAL_ES_PASSWORD must be configured together')
        if username and ':' in username:
            raise ValueError('EVAL_ES_USERNAME must not contain a colon')
        if api_key and username:
            raise ValueError('Configure either ES API key or username/password, not both')
        self.url, self.prefix = url.rstrip('/'), prefix
        self.indices = {'records': records_index or f'{prefix}-records-v2',
                        'samples': samples_index or f'{prefix}-samples-v2',
                        'traces': traces_index or f'{prefix}-traces-v1'}
        if len(set(self.indices.values())) != 3 or any(
                not re.fullmatch(r'[a-z][a-z0-9_-]{0,254}', name) for name in self.indices.values()):
            raise ValueError('ES index names must be valid and distinct')
        if not isinstance(verify_ssl, bool):
            raise ValueError('verify_ssl must be a boolean')
        verify = ssl.create_default_context(cafile=ca_file) if verify_ssl else False
        self.client = client or httpx.Client(timeout=30, trust_env=False, verify=verify,
            headers={'Authorization': 'ApiKey ' + api_key} if api_key else {},
            auth=httpx.BasicAuth(username, password) if username else None)
        if client is not None:
            if username:
                self.client.headers.pop('Authorization', None)
                self.client.auth = httpx.BasicAuth(username, password)
            elif api_key:
                self.client.auth = None
                self.client.headers['Authorization'] = 'ApiKey ' + api_key

    @classmethod
    def from_env(cls):
        verify = os.getenv('EVAL_ES_VERIFY_SSL', 'false').strip().lower()
        if verify not in {'true', 'false', '1', '0'}:
            raise ValueError('EVAL_ES_VERIFY_SSL must be true/false or 1/0')
        return cls(os.getenv('EVAL_ES_URL', 'http://127.0.0.1:9200'),
                   prefix=os.getenv('EVAL_ES_PREFIX', 'evaluation'), api_key=os.getenv('EVAL_ES_API_KEY', ''),
                   username=os.getenv('EVAL_ES_USERNAME', ''), password=os.getenv('EVAL_ES_PASSWORD', ''),
                   verify_ssl=verify in {'true', '1'}, ca_file=os.getenv('EVAL_ES_CA_FILE') or None,
                   records_index=os.getenv('EVAL_ES_RECORDS_INDEX'),
                   samples_index=os.getenv('EVAL_ES_SAMPLES_INDEX'),
                   traces_index=os.getenv('EVAL_ES_TRACES_INDEX'))

    @observed('elasticsearch.request', method='method')
    def call(self, method, path, **kwargs):
        try:
            response = self.client.request(method, self.url + path, **kwargs)
        except httpx.HTTPError as error:
            from evaluation_backend.observability import event
            event('elasticsearch.transport.failed', level='ERROR', error=error, method=method)
            raise StorageError('Elasticsearch is unavailable') from None
        if response.status_code == 404:
            raise NotFound('Record not found')
        if response.status_code == 409:
            raise Conflict('Record already exists or revision has changed')
        if response.status_code == 400:
            try:
                if response.json().get('error', {}).get('type') == 'resource_already_exists_exception':
                    raise Conflict('Index already exists')
            except (ValueError, AttributeError):
                pass
        if response.is_error:
            from evaluation_backend.observability import event
            event('elasticsearch.response.failed', level='ERROR', method=method, status_code=response.status_code)
            raise StorageError('Elasticsearch request failed')
        return response.json()

    def index(self, collection):
        if collection not in COLLECTIONS:
            raise ValueError('Unknown collection')
        return self.indices[collection if collection in ('samples', 'traces') else 'records']

    @staticmethod
    def key(project, ident):
        return hashlib.sha256(json.dumps([project, ident]).encode()).hexdigest()

    @observed('storage.initialize')
    def initialize(self):
        # Keep the existing trace mapping compatible; v2 records have no payload wrapper.
        properties = {name: {'type': 'keyword'} for name in
                      ('project_id', 'id', 'evaluation_id', 'sample_id', 'dataset_id', 'scorer_id', 'status')}
        properties.update(created_at={'type': 'date'}, payload={'type': 'object', 'enabled': False})
        record_properties = {name: {'type': 'keyword'} for name in
            ('project_id', 'id', 'record_type', 'state', 'run_id', 'sample_id', 'dataset_id',
             'environment_id', 'target_id', 'status', 'version_keys')}
        record_properties.update(version={'type': 'integer'}, created_at={'type': 'date'},
                                 updated_at={'type': 'date'}, spec={'type': 'object', 'enabled': False})
        record_properties.update(context_ready={'type': 'boolean'}, summary_ready={'type': 'boolean'})
        sample_properties = {name: {'type': 'keyword'} for name in
            ('project_id', 'id', 'run_id', 'sample_id', 'status', 'trace_id')}
        sample_properties.update(created_at={'type': 'date'}, scores={'type': 'nested', 'dynamic': False,
            'properties': {'scorer_id': {'type': 'keyword'}, 'scorer_version': {'type': 'keyword'},
                           'value': {'type': 'double'}, 'passed': {'type': 'boolean'},
                           'status': {'type': 'keyword'}}})
        for index, mapping in ((self.indices['records'], record_properties),
                               (self.indices['samples'], sample_properties),
                               (self.indices['traces'], properties)):
            try:
                self.call('PUT', '/' + index, json={'mappings': {'dynamic': False, 'properties': mapping}})
            except StorageError:
                # Only ignore the specific concurrent/already-created index case.
                existing = self.call('GET', '/' + index + '/_mapping')
                if not existing:
                    raise

    @staticmethod
    def unpack(hit):
        return {**hit['_source']['payload'], '_revision': f"{hit['_seq_no']}:{hit['_primary_term']}"}

    @observed('storage.read', collection='collection', project_id='project')
    def get(self, collection, project, ident):
        if collection != 'traces':
            return self.get_record(collection, project, ident)
        hit = self.call('GET', f'/{self.index(collection)}/_doc/{self.key(project, ident)}')
        if hit['_source']['project_id'] != project:
            raise NotFound('Record not found')
        return self.unpack(hit)

    @observed('storage.write', collection='collection', project_id='project')
    def put(self, collection, project, ident, value, *, create=False, revision=None):
        if collection != 'traces':
            return self.put_record(collection, project, ident, value, create=create, revision=revision)
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

    @observed('storage.search', collection='collection', project_id='project')
    def search(self, collection, project, *, filters=None, offset=0, limit=100):
        if collection != 'traces':
            return self.search_records(collection, project, filters=filters, offset=offset, limit=limit)
        terms = [{'term': {'project_id': project}}]
        for name, value in (filters or {}).items():
            if name not in {'evaluation_id', 'sample_id', 'dataset_id', 'scorer_id', 'status'}:
                raise ValueError('Unsupported search filter')
            terms.append({'term': {name: value}})
        result = self.call('POST', f'/{self.index(collection)}/_search', json={
            'query': {'bool': {'filter': terms}}, 'sort': [{'id': 'asc'}], 'from': offset,
            'size': limit, 'track_total_hits': True, 'seq_no_primary_term': True})
        return {'items': [self.unpack(h) for h in result['hits']['hits']], 'total': result['hits']['total']['value']}

    def record_key(self, collection, project, ident):
        kind = 'run' if collection in ('experiments', 'run-contexts') else collection
        return self.key(project, [kind, ident])

    @staticmethod
    def revision_params(revision):
        if revision is None:
            return {}
        try:
            seq, term = map(int, revision.split(':'))
        except (ValueError, AttributeError):
            raise Conflict('Invalid revision') from None
        return {'if_seq_no': seq, 'if_primary_term': term}

    def decode(self, collection, hit):
        doc = dict(hit['_source'])
        rev = f"{hit['_seq_no']}:{hit['_primary_term']}"
        if collection == 'targets':
            return {**doc['spec'], 'id': doc['id'], '_revision': rev}
        if collection in ('datasets', 'environments', 'target-configs'):
            if collection == 'target-configs':
                return {**doc['spec'], 'id': doc['id'], 'version': doc['version'], '_revision': rev}
            versions = []
            keys = doc.get('version_keys', [])
            if keys:
                result = self.call('POST', '/' + self.indices['records'] + '/_mget', json={'ids': keys})
                if any(not item.get('found') or item['_source']['project_id'] != doc['project_id'] for item in result['docs']):
                    raise StorageError('Resource version is missing')
                versions = [item['_source']['spec'] for item in result['docs']]
            value = {'id': doc['id'], 'versions': versions, '_revision': rev}
            if collection == 'datasets':
                value['draft'] = doc['spec']
            return value
        doc.pop('project_id', None)
        doc.pop('record_type', None)
        doc.pop('context_ready', None)
        doc.pop('summary_ready', None)
        if 'run_id' in doc:
            doc['evaluation_id'] = doc.pop('run_id')
        if 'execution_config' in doc:
            doc['environment'] = doc.pop('execution_config')
        for old, new in RUN_FIELDS.items():
            if new in doc:
                doc[old] = doc.pop(new)
        if collection == 'samples':
            doc['id'] = doc['sample_id']
            for old, new in SAMPLE_FIELDS.items():
                if new in doc:
                    doc[old] = doc.pop(new)
            doc['scores'] = [decode_score(s) for s in doc.get('scores', [])]
            trace_id = doc.pop('trace_id', None)
            doc['trace'] = []
            if trace_id:
                try:
                    trace = self.get('traces', hit['_source']['project_id'], trace_id)
                    doc['trace'] = trace['data'].get('trace', [])
                except NotFound:
                    pass
        return {**doc, '_revision': rev}

    def get_record(self, collection, project, ident):
        hit = self.call('GET', f'/{self.index(collection)}/_doc/{self.record_key(collection, project, ident)}')
        if hit['_source']['project_id'] != project:
            raise NotFound('Record not found')
        if collection == 'run-contexts' and not hit['_source'].get('context_ready'):
            raise NotFound('Record not found')
        return self.decode(collection, hit)

    def put_record(self, collection, project, ident, value, *, create=False, revision=None):
        if collection == 'scores':
            raise ValueError('Scores must be written with their sample')
        value = {k: v for k, v in value.items() if k != '_revision'}
        key = self.record_key(collection, project, ident)
        index = self.index(collection)
        params = {'refresh': 'wait_for', **self.revision_params(revision)}
        doc = {**value, 'project_id': project, 'id': ident}
        if collection == 'targets':
            doc = {'project_id': project, 'record_type': 'target', 'id': ident, 'state': 'head',
                   'version': value['version'], 'spec': value,
                   'updated_at': datetime.now(timezone.utc).isoformat()}
        elif collection in ('datasets', 'environments', 'target-configs'):
            kind = {'datasets': 'dataset', 'environments': 'environment', 'target-configs': 'target-config'}[collection]
            versions = value.get('versions', [])
            version = len(versions)
            spec = value.get('draft', {})
            if collection == 'target-configs':
                try:
                    current = self.get_record(collection, project, ident)
                except NotFound:
                    current = None
                if current and create:
                    raise Conflict('Record already exists')
                if current and revision != current['_revision']:
                    raise Conflict('Target config changed')
                version = (current['version'] if current else 0) + 1
                spec = {k: v for k, v in value.items() if k not in ('id', 'version')}
                versions = [{**spec, 'version': version}]
            keys = []
            for item in versions:
                # Stage immutable versions before atomically publishing their head pointer.
                # Content-derived IDs make retries safe, including a failed head CAS.
                digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                version_key = self.key(project, [kind, ident, item['version'], digest])
                version_doc = {'project_id': project, 'record_type': kind, 'id': ident,
                               'state': 'published', 'version': item['version'], 'spec': item}
                try:
                    self.call('PUT', f'/{index}/_doc/{version_key}', json=version_doc,
                              params={'op_type': 'create', 'refresh': 'wait_for'})
                except Conflict:
                    pass
                keys.append(version_key)
            doc = {'project_id': project, 'record_type': kind, 'id': ident, 'state': 'head',
                   'version': version, 'spec': spec, 'version_keys': keys,
                   'name': spec.get('name', versions[-1].get('name', ident) if versions else ident),
                   'updated_at': datetime.now(timezone.utc).isoformat()}
        elif collection == 'samples':
            doc.pop('trace', None)
            doc['trace_id'] = ident
            doc['run_id'] = doc.pop('evaluation_id')
            for old, new in SAMPLE_FIELDS.items():
                if old in doc:
                    doc[new] = doc.pop(old)
            doc['scores'] = [encode_score(s) for s in doc.get('scores', [])]
        else:
            doc['record_type'] = 'run'
            doc['run_id'] = ident
            doc.pop('evaluation_id', None)
            if 'environment' in doc:
                doc['execution_config'] = doc.pop('environment')
            for old, new in RUN_FIELDS.items():
                if old in doc:
                    doc[new] = doc.pop(old)
            marker = 'summary_ready' if collection == 'experiments' else 'context_ready'
            doc[marker] = True
            defaults = {'status': 'RUNNING', 'completed_samples': 0, 'failed_samples': 0}
            # Atomic field merge: a result update must never overwrite the pinned context.
            script = {'lang': 'painless', 'source':
                "if (params.create && ctx._source.containsKey(params.marker)) { ctx.op = 'none'; } "
                "else { for (def e : params.doc.entrySet()) { "
                "if (e.getKey() != 'created_at' || !ctx._source.containsKey('created_at')) { "
                "ctx._source[e.getKey()] = e.getValue(); } } }",
                'params': {'doc': doc, 'create': create, 'marker': marker}}
            hit = self.call('POST', f'/{index}/_update/{key}', params={**params, 'retry_on_conflict': 5} if revision is None else params,
                            json={'script': script, 'upsert': {**defaults, **doc}})
            if hit.get('result') == 'noop':
                raise Conflict('Record already exists')
            return {**value, '_revision': f"{hit['_seq_no']}:{hit['_primary_term']}"}
        if create:
            params['op_type'] = 'create'
        hit = self.call('PUT', f'/{index}/_doc/{key}', json=doc, params=params)
        return {**value, **({'version': version} if collection == 'target-configs' else {}),
                '_revision': f"{hit['_seq_no']}:{hit['_primary_term']}"}

    def search_records(self, collection, project, *, filters=None, offset=0, limit=100):
        terms = [{'term': {'project_id': project}}]
        if collection == 'targets':
            terms.extend([{'term': {'record_type': 'target'}}, {'term': {'state': 'head'}}])
        elif collection in ('datasets', 'environments', 'target-configs'):
            kind = {'datasets': 'dataset', 'environments': 'environment', 'target-configs': 'target-config'}[collection]
            terms.extend([{'term': {'record_type': kind}}, {'term': {'state': 'head'}}])
        elif collection in ('experiments', 'run-contexts'):
            terms.append({'term': {'record_type': 'run'}})
            if collection == 'run-contexts':
                terms.append({'term': {'context_ready': True}})
        elif collection != 'samples':
            raise ValueError('Use sample scores instead of a scores collection')
        for name, value in (filters or {}).items():
            if name not in {'evaluation_id', 'sample_id', 'dataset_id', 'status'}:
                raise ValueError('Unsupported search filter')
            terms.append({'term': {'run_id' if name == 'evaluation_id' else name: value}})
        result = self.call('POST', f'/{self.index(collection)}/_search', json={
            'query': {'bool': {'filter': terms}}, 'sort': [{'id': 'asc'}], 'from': offset,
            'size': limit, 'track_total_hits': True, 'seq_no_primary_term': True})
        return {'items': [self.decode(collection, h) for h in result['hits']['hits']],
                'total': result['hits']['total']['value']}


SAMPLE_FIELDS = {'latencyMs': 'latency_ms', 'modelLatencyMs': 'model_latency_ms', 'costUsd': 'cost_usd'}
RUN_FIELDS = {'items': 'completed_samples', 'errors': 'failed_samples'}


def encode_score(score):
    result = dict(score)
    for old, new in (('scorerId', 'scorer_id'), ('scorerVersion', 'scorer_version')):
        if old in result:
            result[new] = result.pop(old)
    result.setdefault('status', 'completed')
    return result


def decode_score(score):
    result = dict(score)
    for new, old in (('scorerId', 'scorer_id'), ('scorerVersion', 'scorer_version')):
        if old in result:
            result[new] = result.pop(old)
    return result

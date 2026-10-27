"""Docker Engine adapter for server-rendered evaluation workloads (no kubectl)."""
import json
import os
import struct
import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import quote
import httpx
from fastapi import HTTPException
from evaluation_backend.observability import observed, event

OWNER = 'evaluation.backend.owner'
RECORD = 'evaluation.backend.job'
NETWORK = 'evaluation.backend.network'


class DockerRuntime:
    namespace = 'docker'
    jobs = '/apis/batch/v1/namespaces/docker/jobs'

    def __init__(self, client=None):
        self.network = os.getenv('EVAL_DOCKER_NETWORK', 'evaluation-compose')
        self.client = client

    @observed('docker.request', method='method')
    def request(self, method, path, **kwargs):
        try:
            connection = nullcontext(self.client) if self.client is not None else httpx.Client(
                base_url='http://docker/v1.45', transport=httpx.HTTPTransport(
                    uds=os.getenv('EVAL_DOCKER_SOCKET', '/var/run/docker.sock')), timeout=30, trust_env=False)
            with connection as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            event('docker.transport.failed', level='ERROR', error=error)
            raise HTTPException(502, 'Docker Engine is unavailable') from None
        if response.status_code == 404: raise HTTPException(404, 'Evaluation not found')
        if response.status_code == 409: raise HTTPException(409, 'Evaluation already exists')
        if response.is_error:
            event('docker.response.failed', level='ERROR', status_code=response.status_code)
            raise HTTPException(502, 'Docker execution request failed')
        return response

    def inspect(self, ident):
        if ident.startswith('eval-'):
            ident = self.container_name(ident)
        value = self.request('GET', '/containers/' + quote(ident, safe='') + '/json').json()
        labels = value.get('Config', {}).get('Labels', {})
        if labels.get(OWNER) != 'evaluation-backend' or labels.get(NETWORK) != self.network:
            raise HTTPException(404, 'Evaluation not found')
        return value

    def container_name(self, job_name):
        return 'runner-' + hashlib.sha256(self.network.encode()).hexdigest()[:10] + '-' + job_name

    def job(self, container):
        record = json.loads(container['Config']['Labels'][RECORD])
        state = container['State']
        if state.get('Running') and state.get('StartedAt'):
            started = datetime.fromisoformat(state['StartedAt'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - started).total_seconds() > record['spec'].get('activeDeadlineSeconds', 600):
                self.request('POST', '/containers/' + container['Id'] + '/stop', params={'t': 5})
                state = self.inspect(container['Id'])['State']
                event('docker.workload.timed_out', level='ERROR', job_name=record['metadata']['name'])
        record['metadata']['creationTimestamp'] = container['Created']
        record['status'] = {}
        if state.get('Running'): record['status']['active'] = 1
        elif state.get('Status') in ('exited', 'dead'):
            record['status']['conditions'] = [{'type': 'Complete' if state.get('ExitCode') == 0 else 'Failed', 'status': 'True'}]
        return record

    def runtime_env(self):
        names = {'EVAL_MODEL_RESOLVER_URL', 'EVAL_MODEL_RESOLVER_TOKEN', 'EVAL_MODEL_RESOLVER_TOKEN_FILE',
                 'EVAL_STORAGE_API_URL', 'EVAL_COLLECTOR_EXPORTER', 'EVAL_LOG_LEVEL', 'EVAL_CONCURRENCY',
                 'EVAL_LLM_PROXY_ENABLED', 'EVAL_PLATFORM', 'EVAL_MODEL_ADAPTER'}
        env = {k: v for k, v in os.environ.items() if k in names or k.startswith(('EVAL_ES_', 'OTEL_'))}
        files = {k: v for k, v in env.items() if k.endswith('_FILE') and v.startswith('/')}
        binds = []
        if files:
            host = self.request('GET', '/containers/' + quote(os.environ['HOSTNAME'], safe='') + '/json').json()
            mounts = {m['Destination']: m['Source'] for m in host.get('Mounts', [])}
            for key, destination in files.items():
                if destination not in mounts: raise HTTPException(503, 'Runner credential file mount is missing')
                binds.append(mounts[destination] + ':' + destination + ':ro')
                env.pop(key.removesuffix('_FILE'), None)
        return env, binds

    def create(self, job):
        container = job['spec']['template']['spec']['containers'][0]
        env, binds = self.runtime_env()
        env.update({v['name']: v['value'] for v in container.get('env', []) if 'value' in v})
        resources = container.get('resources', {}).get('limits', {})
        cpu = str(resources.get('cpu', '2000m'))
        cpu = float(cpu[:-1]) / 1000 if cpu.endswith('m') else float(cpu)
        memory = str(resources.get('memory', '2048Mi'))
        memory = int(memory[:-2]) * 1024 * 1024 if memory.endswith('Mi') else int(memory)
        # Labels persist public metadata only. No environment or credentials.
        record = {'metadata': job['metadata'], 'spec': {'activeDeadlineSeconds': job['spec'].get('activeDeadlineSeconds', 600)}}
        name = job['metadata']['name']
        labels = {OWNER: 'evaluation-backend', RECORD: json.dumps(record), NETWORK: self.network,
                  'evaluation.job': name, 'evaluation.project': job['metadata']['labels']['project']}
        body = {'Image': container['image'], 'Entrypoint': container['command'], 'Cmd': [],
                'User': '10001:10001', 'WorkingDir': '/home/worker', 'Labels': labels,
                'Env': [k + '=' + v for k, v in env.items()], 'HostConfig': {
                    'NetworkMode': self.network, 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                    'SecurityOpt': ['no-new-privileges:true'], 'Binds': binds, 'Memory': memory,
                    'NanoCpus': int(cpu * 1_000_000_000), 'PidsLimit': 256,
                    'Tmpfs': {'/tmp': 'rw,nosuid,size=134217728,uid=10001,gid=10001',
                              '/home/worker': 'rw,nosuid,size=134217728,uid=10001,gid=10001'},
                    'RestartPolicy': {'Name': 'no'}}}
        created = self.request('POST', '/containers/create', params={'name': self.container_name(name)}, json=body).json()
        self.request('POST', '/containers/' + created['Id'] + '/start')
        event('docker.workload.created', job_name=name)
        return self.job(self.inspect(created['Id']))

    def logs(self, name, tail):
        container = self.inspect(name)
        data = self.request('GET', '/containers/' + container['Id'] + '/logs',
            params={'stdout': 'true', 'stderr': 'true', 'tail': str(tail)}).content
        parts, offset = [], 0
        while offset + 8 <= len(data) and data[offset] in (0, 1, 2):
            length = struct.unpack('>I', data[offset + 4:offset + 8])[0]
            if offset + 8 + length > len(data): break
            parts.append(data[offset + 8:offset + 8 + length]); offset += 8 + length
        return (b''.join(parts) if parts else data).decode(errors='replace')

    def call(self, method, path, **kwargs):
        if method == 'POST' and path == self.jobs: return self.create(kwargs['json'])
        if method == 'GET' and path == self.jobs:
            filters = {'label': [OWNER + '=evaluation-backend', NETWORK + '=' + self.network]}
            for part in kwargs.get('params', {}).get('labelSelector', '').split(','):
                if part.startswith('project='): filters['label'].append('evaluation.project=' + part.split('=', 1)[1])
            values = self.request('GET', '/containers/json', params={'all': 'true', 'filters': json.dumps(filters)}).json()
            return {'items': [self.job(self.inspect(item['Id'])) for item in values]}
        if method == 'GET' and path.startswith(self.jobs + '/'):
            return self.job(self.inspect(path.rsplit('/', 1)[1]))
        if method == 'GET' and path.endswith('/pods'):
            name = kwargs.get('params', {}).get('labelSelector', '').removeprefix('job-name=')
            return {'items': [{'metadata': {'name': self.inspect(name)['Id']}}]}
        if method == 'GET' and path.endswith('/log'):
            return self.logs(path.split('/')[-2], kwargs.get('params', {}).get('tailLines', 100))
        raise HTTPException(400, 'Unsupported Docker execution operation')

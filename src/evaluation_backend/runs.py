"""Authenticated, project-scoped evaluation Jobs. Kubernetes stores run snapshots."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict

from evaluation_backend.k8s_job import build_job

router = APIRouter(prefix='/api/v1/evaluations')
PREFIX = 'evaluation.tasklattice.io/'


def authorize(authorization: str = Header(default='')):
    token = os.getenv('EVAL_TRIGGER_TOKEN', '')
    if not token:
        raise HTTPException(503, 'Evaluation trigger is not configured')
    if not hmac.compare_digest(authorization, 'Bearer ' + token):
        raise HTTPException(401, 'Unauthorized')


class StartRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    target_id: str
    dataset_id: str
    dataset_version: int
    environment_id: str
    model_id: str
    scorer_ids: list[str]
    scoring_mode: Literal['mock', 'installed']
    snapshot: dict


def bindings():
    # Only server-owned bindings may choose real resources; never accept Pod specs.
    value = os.getenv('EVAL_RESOURCE_BINDINGS', '')
    if not value:
        raise HTTPException(503, 'Evaluation resource bindings are not configured')
    return json.loads(value)


class Kubernetes:
    def __init__(self):
        root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
        self.token_file = root / 'token'
        self.base = 'https://' + os.environ['KUBERNETES_SERVICE_HOST'] + ':' + os.getenv('KUBERNETES_SERVICE_PORT', '443')
        self.namespace = os.getenv('EVAL_K8S_NAMESPACE', 'evaluation')
        self.ca = str(root / 'ca.crt')

    def call(self, method, path, **kwargs):
        import ssl
        with httpx.Client(verify=ssl.create_default_context(cafile=self.ca), trust_env=False, timeout=20) as client:
            response = client.request(method, self.base + path,
                headers={'Authorization': 'Bearer ' + self.token_file.read_text().strip()}, **kwargs)
        if response.status_code == 404:
            raise HTTPException(404, 'Evaluation not found')
        if response.status_code == 409:
            raise HTTPException(409, 'Evaluation already exists')
        if response.is_error:
            raise HTTPException(502, 'Kubernetes request failed')
        return response.text if path.endswith('/log') else response.json() if response.content else {}

    @property
    def jobs(self):
        return f'/apis/batch/v1/namespaces/{self.namespace}/jobs'


def kube():
    return Kubernetes()


def project_label(project):
    return hashlib.sha256(project.encode()).hexdigest()[:32]


def owned(job, project):
    if job['metadata'].get('annotations', {}).get(PREFIX + 'project') != project:
        raise HTTPException(404, 'Evaluation not found')
    return job


def summary(job, cluster):
    meta = job['metadata']
    annotation = meta.get('annotations', {})
    record = json.loads(annotation[PREFIX + 'record'])
    conditions = {c['type'] for c in job.get('status', {}).get('conditions', []) if c.get('status') == 'True'}
    status = 'COMPLETED' if 'Complete' in conditions else 'FAILED' if 'Failed' in conditions else 'RUNNING'
    if job.get('spec', {}).get('suspend'):
        status = 'CANCELLED'
    result = {}
    error = ''
    if status in ('COMPLETED', 'FAILED'):
        pods = cluster.call('GET', f'/api/v1/namespaces/{cluster.namespace}/pods', params={'labelSelector': 'job-name=' + meta['name']})
        if pods.get('items'):
            pod = pods['items'][0]
            # Only expose the worker's structured summary, never raw upstream logs.
            logs = cluster.call('GET', f"/api/v1/namespaces/{cluster.namespace}/pods/{pod['metadata']['name']}/log", params={'tailLines': 100})
            for line in str(logs).splitlines():
                try:
                    value = json.loads(line)
                    if isinstance(value, dict) and value.get('evaluation_id') == meta['labels']['experiment_id']:
                        result = value
                except (ValueError, TypeError):
                    pass
        if status == 'FAILED':
            error = 'Worker failed. Check the evaluation Job logs for details.'
    report_url = result.get('url')
    if report_url and os.getenv('EVAL_PUBLIC_LANGFUSE_URL'):
        parsed = urlparse(report_url)
        report_url = os.environ['EVAL_PUBLIC_LANGFUSE_URL'].rstrip('/') + parsed.path + ('?' + parsed.query if parsed.query else '')
    return {**record, 'id': meta['name'], 'created_at': meta.get('creationTimestamp'), 'status': status,
            'experiment_id': meta['labels']['experiment_id'], 'completed': result.get('items', 0),
            'report_url': report_url, 'error': error, 'phase': 'RUNNING' if job.get('status', {}).get('active') else 'QUEUED',
            'langfuse_experiment_id': result.get('langfuse_experiment_id')}


@router.get('/config', dependencies=[Depends(authorize)])
def configuration():
    return {'runtime': 'kubernetes', 'bindings': bindings(), 'scoring_modes': ['mock'],
            'target_mode': 'llm', 'scorer_ids': ['mock-score']}


@router.post('/runs', dependencies=[Depends(authorize)])
def start(request: StartRun, x_project_id: str = Header(min_length=1),
          idempotency_key: str = Header(min_length=16, max_length=128), cluster=Depends(kube)):
    binding = next((b for b in bindings() if b['target_id'] == request.target_id
                    and b['dataset_id'] == request.dataset_id and b['dataset_version'] == request.dataset_version
                    and b['environment_id'] == request.environment_id and b['model_id'] == request.model_id), None)
    if not binding:
        raise HTTPException(422, 'Selected target, dataset version, environment or model has no backend binding')
    if request.scoring_mode != 'mock' or request.scorer_ids != ['mock-score']:
        raise HTTPException(422, 'Only explicitly selected mock-score is currently installed')
    if len(json.dumps(request.snapshot).encode()) > 32000:
        raise HTTPException(422, 'Configuration snapshot is too large')
    record = request.model_dump()
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    name = 'eval-' + hashlib.sha256((x_project_id + ':' + idempotency_key).encode()).hexdigest()[:24]
    try:
        previous = owned(cluster.call('GET', cluster.jobs + '/' + name), x_project_id)
    except HTTPException as error:
        if error.status_code != 404:
            raise
    else:
        if previous['metadata']['annotations'][PREFIX + 'request-hash'] != digest:
            raise HTTPException(409, 'Idempotency key was used for different configuration')
        return summary(previous, cluster)
    job = build_job(binding['langfuse_dataset_id'], request.model_id, request.scorer_ids,
                    namespace=cluster.namespace, image=os.environ['EVAL_WORKER_IMAGE'], mock=True)
    job['metadata']['name'] = name
    job['metadata']['labels']['project'] = project_label(x_project_id)
    job['metadata']['annotations'] = {PREFIX + 'project': x_project_id, PREFIX + 'record': json.dumps(record), PREFIX + 'request-hash': digest}
    # UI versions map to immutable Langfuse "as of" timestamps in server config.
    version = binding.get('langfuse_dataset_version') or datetime.now(timezone.utc).isoformat()
    job['spec']['template']['spec']['containers'][0]['env'].append({'name': 'DATASET_VERSION', 'value': version})
    # Retain runs for history; explicit cleanup can be configured by operators.
    job['spec'].pop('ttlSecondsAfterFinished', None)
    try:
        created = cluster.call('POST', cluster.jobs, json=job)
    except HTTPException as error:
        if error.status_code != 409:
            raise
        created = owned(cluster.call('GET', cluster.jobs + '/' + name), x_project_id)
        if created['metadata']['annotations'][PREFIX + 'request-hash'] != digest:
            raise HTTPException(409, 'Idempotency conflict')
    return summary(created, cluster)


@router.get('/runs', dependencies=[Depends(authorize)])
def list_runs(x_project_id: str = Header(min_length=1), cluster=Depends(kube)):
    jobs = cluster.call('GET', cluster.jobs, params={'labelSelector': 'app=evaluation-worker,project=' + project_label(x_project_id)})
    return {'items': [summary(owned(j, x_project_id), cluster) for j in jobs.get('items', [])]}


@router.get('/runs/{name}', dependencies=[Depends(authorize)])
def get_run(name: str, x_project_id: str = Header(min_length=1), cluster=Depends(kube)):
    if not name.startswith('eval-') or not all(c.isalnum() or c == '-' for c in name):
        raise HTTPException(404, 'Evaluation not found')
    return summary(owned(cluster.call('GET', cluster.jobs + '/' + name), x_project_id), cluster)

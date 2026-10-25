"""Authenticated, project-scoped evaluation Jobs. Kubernetes stores run snapshots."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict

from evaluation_backend.k8s_job import build_job, PodResources
from evaluation_backend.storage.service import get_service
from evaluation_backend.storage import NotFound, Conflict
from evaluation_backend.content_policy import ContentPolicy
from evaluation_backend.run_auth import issue_worker_token, verify_worker_token, worker_boundary
from evaluation_backend.uploads import get_upload_store, UploadError

router = APIRouter(prefix='/api/v1/evaluations')
PREFIX = 'evaluation.tasklattice.io/'


async def authorize(request: Request, authorization: str = Header(default='')):
    token = os.getenv('EVAL_TRIGGER_TOKEN', '')
    if not token: raise HTTPException(503, 'Evaluation trigger is not configured')
    if hmac.compare_digest(authorization, 'Bearer ' + token): return
    claims = verify_worker_token(authorization.removeprefix('Bearer '))
    await worker_boundary(request, claims)


class StartRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default="", max_length=200)
    target_id: str
    dataset_id: str
    dataset_version: int
    environment_id: str
    environment_version: int = Field(default=1, ge=1, strict=True)
    model_id: str
    scorer_ids: list[str]
    scoring_mode: Literal['mock', 'installed']
    snapshot: dict
    resources: PodResources | None = None
    upload_id: str | None = Field(default=None, pattern=r"^upload-[a-f0-9]{32}$")


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


def summary(job, cluster, service=None, project=None):
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
    if service is not None and status in ('COMPLETED', 'FAILED', 'CANCELLED'):
        from evaluation_backend.upload_api import cleanup_run
        cleanup_run(service, project, meta['labels']['experiment_id'])
    completed = result.get('items', 0)
    if service is not None:
        experiment_id = meta['labels']['experiment_id']
        page = service.page('samples', project, filters={'evaluation_id': experiment_id}, limit=1)
        completed = page['total']
        try:
            stored = service.storage.get('experiments', project, experiment_id)
            if stored['status'] == 'FAILED':
                status = 'FAILED'
        except NotFound:
            pass
    return {**record, 'id': meta['name'], 'created_at': meta.get('creationTimestamp'), 'status': status,
            'experiment_id': meta['labels']['experiment_id'], 'completed': completed,
            'error': error, 'phase': status if status != 'RUNNING' else 'RUNNING' if job.get('status', {}).get('active') else 'QUEUED'}


@router.get('/targets', dependencies=[Depends(authorize)])
def target_catalog():
    from evaluation_backend.targets import targets
    values = targets()
    return {'items': values, 'total': len(values)}


@router.get('/config', dependencies=[Depends(authorize)])
def configuration():
    return {'runtime': 'kubernetes', 'bindings': bindings(), 'scoring_modes': ['mock', 'installed'],
            'target_mode': 'llm', 'scorer_ids': ['mock-score', 'sc-risk']}


@router.post('/runs', dependencies=[Depends(authorize)])
def start(request: StartRun, x_project_id: str = Header(min_length=1),
          idempotency_key: str = Header(min_length=16, max_length=128), cluster=Depends(kube), service=Depends(get_service)):
    binding = next((b for b in bindings() if b['target_id'] == request.target_id
                    and b['dataset_id'] == request.dataset_id and b['dataset_version'] == request.dataset_version
                    and b['environment_id'] == request.environment_id and b['model_id'] == request.model_id), None)
    if not binding:
        raise HTTPException(422, 'Selected target, dataset version, environment or model has no backend binding')
    # Idempotency is checked before touching temporary content (which may already be deleted).
    digest = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True).encode()).hexdigest()
    name = 'eval-' + hashlib.sha256((x_project_id + ':' + idempotency_key).encode()).hexdigest()[:24]
    experiment = 'exp-' + hashlib.sha256((x_project_id + ':' + idempotency_key).encode()).hexdigest()[:32]
    try:
        previous = owned(cluster.call('GET', cluster.jobs + '/' + name), x_project_id)
    except HTTPException as error:
        if error.status_code != 404: raise
    else:
        if previous['metadata']['annotations'][PREFIX + 'request-hash'] != digest:
            raise HTTPException(409, 'Idempotency key was used for different configuration')
        return summary(previous, cluster, service, x_project_id)
    from evaluation_backend.environments import environment_version
    from evaluation_backend.targets import targets
    environment = environment_version(service, x_project_id, request.environment_id, request.environment_version)
    resources = PodResources(cpu_millicores=environment['cpuMillicores'], memory_mib=environment['memoryMiB'])
    if request.resources is not None and request.resources != resources:
        raise HTTPException(422, 'Resources must match the stored environment version')
    target = next((t for t in targets() if t['id'] == request.target_id), None)
    if target is None: raise HTTPException(422, 'Target is not configured')
    version = service.dataset_version(x_project_id, request.dataset_id, request.dataset_version)
    if request.scoring_mode == 'mock':
        if request.scorer_ids != ['mock-score']: raise HTTPException(422, 'Mock mode requires only mock-score')
    else:
        from evaluation_backend.experiment import Dataset
        from evaluation_backend.scorers import InstalledScorerLibrary
        try:
            if not request.scorer_ids or len(request.scorer_ids) != len(set(request.scorer_ids)): raise ValueError('Scorers must be nonempty and unique')
            allowed = version.get('scorerIds', [])
            if allowed and any(s not in allowed for s in request.scorer_ids): raise ValueError('Scorer is not enabled for this dataset version')
            InstalledScorerLibrary().get_scorers(Dataset(request.dataset_id, version['name'], version['metadata']), request.scorer_ids)
        except ValueError as error: raise HTTPException(422, str(error)) from None
    count = len(version['rows'])
    if request.upload_id:
        try: count = get_upload_store().claim(x_project_id, request.upload_id, request.dataset_id, request.dataset_version, name, experiment)
        except UploadError as error: raise HTTPException(422, str(error)) from None
    context = {'id': experiment, 'target': target, 'environment': environment, 'dataset_id': request.dataset_id, 'dataset_version': request.dataset_version,
               'upload_id': request.upload_id, 'content_policy': ContentPolicy.from_env().model_dump(), 'request_hash': digest}
    try: service.storage.put('run-contexts', x_project_id, experiment, context, create=True)
    except Conflict:
        context = service.storage.get('run-contexts', x_project_id, experiment)
        if context['request_hash'] != digest: raise HTTPException(409, 'Idempotency conflict')
    target = context.get('target', target)
    environment = context.get('environment', environment)
    resources = PodResources(cpu_millicores=environment['cpuMillicores'], memory_mib=environment['memoryMiB'])
    # Do not put caller-supplied rows or arbitrary snapshot data in Kubernetes annotations.
    snapshot = {'target': target, 'datasetId': request.dataset_id,
                'dataset': {k: version[k] for k in ('name', 'version', 'publishedAt', 'fields')},
                'environmentId': request.environment_id,
                'environment': {**environment, 'modelId': request.model_id, 'runner': {'id': 'kubernetes', 'concurrency': 1}},
                'scorers': []}
    snapshot['dataset'].update(rows=[], objective='', description='', tags=[], metadata={}, scorerIds=[])
    record = {k: v for k,v in request.model_dump().items() if k not in ('snapshot', 'upload_id')}
    from zoneinfo import ZoneInfo
    generated_name = target['name'][:150] + ' · ' + datetime.now(ZoneInfo(os.getenv('EVAL_NAME_TIMEZONE', 'Asia/Shanghai'))).strftime('%Y-%m-%d %H:%M:%S')
    record.update(name=request.name.strip() or generated_name, snapshot=snapshot, sample_count=count, resources=resources.model_dump())
    job = build_job(request.dataset_id, request.model_id, request.scorer_ids,
                    namespace=cluster.namespace, image=os.environ['EVAL_WORKER_IMAGE'], mock=request.scoring_mode == 'mock', experiment_id=experiment, resources=resources)
    job['metadata']['name'] = name
    job['metadata']['labels']['project'] = project_label(x_project_id)
    job['metadata']['annotations'] = {PREFIX + 'project': x_project_id, PREFIX + 'record': json.dumps(record), PREFIX + 'request-hash': digest}
    job['spec']['template']['spec']['containers'][0]['env'].extend([
        {'name': 'EVAL_DATASET_VERSION', 'value': str(request.dataset_version)},
        {'name': 'EVAL_PLATFORM', 'value': 'storage'},
        {'name': 'EVAL_PROJECT_ID', 'value': x_project_id},
        {'name': 'EVAL_RUN_INPUT_ID', 'value': experiment},
        {'name': 'EVAL_STORAGE_TOKEN', 'value': issue_worker_token(x_project_id, experiment)},
        {'name': 'EVAL_TARGET_SNAPSHOT', 'value': json.dumps(target)},
        {'name': 'EVAL_CONTENT_POLICY', 'value': json.dumps(context['content_policy'])}])
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
    return summary(created, cluster, service, x_project_id)


@router.get('/runs', dependencies=[Depends(authorize)])
def list_runs(x_project_id: str = Header(min_length=1), cluster=Depends(kube), service=Depends(get_service)):
    jobs = cluster.call('GET', cluster.jobs, params={'labelSelector': 'app=evaluation-worker,project=' + project_label(x_project_id)})
    return {'items': [summary(owned(j, x_project_id), cluster, service, x_project_id) for j in jobs.get('items', [])]}


@router.get('/runs/{name}', dependencies=[Depends(authorize)])
def get_run(name: str, x_project_id: str = Header(min_length=1), cluster=Depends(kube), service=Depends(get_service)):
    if not name.startswith('eval-') or not all(c.isalnum() or c == '-' for c in name):
        raise HTTPException(404, 'Evaluation not found')
    return summary(owned(cluster.call('GET', cluster.jobs + '/' + name), x_project_id), cluster, service, x_project_id)

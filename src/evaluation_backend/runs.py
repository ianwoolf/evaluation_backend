"""Authenticated, project-scoped evaluation Jobs. Kubernetes stores run snapshots."""
from evaluation_backend.observability import observed
from evaluation_backend.observability import event
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
    if (os.getenv('EVAL_AUTH_MODE', 'token') == 'none' and not authorization
            and os.getenv('EVAL_COLLECTOR_LOCAL') != 'true'
            and request.url.path.startswith('/api/v1/evaluations/')):
        return
    token = os.getenv('EVAL_TRIGGER_TOKEN', '')
    if not token: raise HTTPException(503, 'Evaluation trigger is not configured')
    if hmac.compare_digest(authorization, 'Bearer ' + token): return
    claims = verify_worker_token(authorization.removeprefix('Bearer '))
    await worker_boundary(request, claims)


class DatasetSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: int = Field(ge=1, strict=True)
    upload_id: str | None = Field(default=None, pattern=r'^upload-[a-f0-9]{32}$')


class StartRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    execution_mode: Literal['legacy', 'guardrail'] = 'legacy'
    guardrail_bindings: list[dict] | None = None
    config_revision: str | None = None
    guardrail_provider: Literal['f5','nemo guardrail'] | None = None
    guardrail_instance_id: str | None = None
    name: str = Field(default="", max_length=200)
    target_id: str
    dataset_id: str
    dataset_version: int
    environment_id: str
    environment_version: int = Field(default=1, ge=1, strict=True)
    model_id: str
    scorer_ids: list[str]
    scorer_selection: Literal['shared','dataset'] = 'shared'
    scoring_mode: Literal['mock', 'installed']
    snapshot: dict
    resources: PodResources | None = None
    upload_id: str | None = Field(default=None, pattern=r"^upload-[a-f0-9]{32}$")
    datasets: list[DatasetSelection] | None = Field(default=None, min_length=1, max_length=20)


class Kubernetes:
    def __init__(self):
        root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
        self.token_file = root / 'token'
        self.base = 'https://' + os.environ['KUBERNETES_SERVICE_HOST'] + ':' + os.getenv('KUBERNETES_SERVICE_PORT', '443')
        self.namespace = os.getenv('EVAL_K8S_NAMESPACE', 'evaluation')
        self.ca = str(root / 'ca.crt')

    @observed('kubernetes.request', method='method')
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
    if os.getenv('EVAL_EXECUTION_BACKEND', 'kubernetes') == 'docker':
        from evaluation_backend.docker_runtime import DockerRuntime
        return DockerRuntime()
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
            'runtime': os.getenv('EVAL_EXECUTION_BACKEND', 'kubernetes'),
            'container_name': cluster.container_name(meta['name']) if hasattr(cluster, 'container_name') else None,
            'experiment_id': meta['labels']['experiment_id'], 'completed': completed,
            'error': error, 'phase': status if status != 'RUNNING' else 'RUNNING' if job.get('status', {}).get('active') else 'QUEUED'}


@router.get('/config', dependencies=[Depends(authorize)])
def configuration():
    return {'runtime': os.getenv('EVAL_EXECUTION_BACKEND', 'kubernetes'), 'scoring_modes': ['mock', 'installed'],
            'target_mode': 'llm', 'scorer_ids': ['mock-score', 'sc-risk']}


@router.post('/runs', dependencies=[Depends(authorize)])
@observed('evaluation.trigger', project_id='x_project_id')
def start(request: StartRun, x_project_id: str = Header(min_length=1),
          idempotency_key: str = Header(min_length=16, max_length=128), cluster=Depends(kube), service=Depends(get_service)):
    if request.datasets is not None and request.upload_id:
        raise HTTPException(422, 'Specify uploads per dataset')
    # Idempotency is checked before touching temporary content (which may already be deleted).
    digest = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True).encode()).hexdigest()
    name = 'eval-' + hashlib.sha256((x_project_id + ':' + idempotency_key).encode()).hexdigest()[:24]
    experiment = 'exp-' + hashlib.sha256((x_project_id + ':' + idempotency_key).encode()).hexdigest()[:32]
    event('evaluation.identified', project_id=x_project_id, run_id=experiment, job_name=name,
          dataset_id=request.dataset_id, target_id=request.target_id, environment_id=request.environment_id)
    try:
        previous = owned(cluster.call('GET', cluster.jobs + '/' + name), x_project_id)
    except HTTPException as error:
        if error.status_code != 404: raise
    else:
        if previous['metadata']['annotations'][PREFIX + 'request-hash'] != digest:
            raise HTTPException(409, 'Idempotency key was used for different configuration')
        event('evaluation.trigger.reused', run_id=experiment, job_name=name)
        return summary(previous, cluster, service, x_project_id)
    from evaluation_backend.environments import environment_version
    from evaluation_backend.targets import get_target
    if service.is_archived(x_project_id,'environments',request.environment_id):raise HTTPException(422,'Environment is archived; select an active environment')
    environment = environment_version(service, x_project_id, request.environment_id, request.environment_version)
    if request.model_id != environment['modelId']:
        raise HTTPException(422, 'Model must match the stored environment version')
    concurrency=environment.get('runner',{}).get('concurrency',1)
    if type(concurrency) is not int or not 1<=concurrency<=32:raise HTTPException(422,'Environment case concurrency must be 1..32')
    resources = PodResources(cpu_millicores=environment['cpuMillicores'], memory_mib=environment['memoryMiB'])
    if request.resources is not None and request.resources != resources:
        raise HTTPException(422, 'Resources must match the stored environment version')
    target = get_target(service, x_project_id, request.target_id)
    if service.is_archived(x_project_id, 'targets', request.target_id): raise HTTPException(422, 'Evaluation is archived')
    stored_target=service.storage.get('targets',x_project_id,request.target_id)
    if stored_target.get('evaluation_config',{}).get('execution_mode')=='guardrail' and request.execution_mode!='guardrail':
        raise HTTPException(422,'Trigger this Guardrail Evaluation using its saved configuration')
    if request.execution_mode == 'guardrail':
        if request.scoring_mode!='installed':raise HTTPException(422,'Use the configured installed scoring mode')
        from evaluation_backend.target_configs import load_config, configured_datasets
        config = load_config(service, x_project_id, request.target_id)
        if request.config_revision != config['_revision']: raise Conflict('Evaluation configuration changed; reload before triggering')
        if config.get('execution_mode') != 'guardrail' or (request.environment_id, request.environment_version) != (config['environment_id'], config['environment_version']):
            raise HTTPException(422, 'Run must use the configured environment and execution mode')
        allowed = {(d['dataset_id'],d['dataset_version']) for d in configured_datasets(config)}
        requested = request.datasets or [DatasetSelection(dataset_id=request.dataset_id,dataset_version=request.dataset_version)]
        if any(s.upload_id or (s.dataset_id,s.dataset_version) not in allowed for s in requested): raise HTTPException(422, 'Select only configured dataset versions')
        if len(requested)!=len(allowed) or {(s.dataset_id,s.dataset_version) for s in requested}!=allowed:raise HTTPException(422,'Use all datasets from the configured Evaluation version')
        target={**target,'provider':request.guardrail_provider or target.get('provider'),'guardrail_id':request.guardrail_instance_id or target.get('guardrail_id')}
        request.guardrail_bindings = config.get('guardrail_bindings')

    selections = request.datasets or [DatasetSelection(dataset_id=request.dataset_id,
        dataset_version=request.dataset_version, upload_id=request.upload_id)]
    if len({s.dataset_id for s in selections}) != len(selections):
        raise HTTPException(422, 'Select each dataset only once')
    if (selections[0].dataset_id, selections[0].dataset_version) != (request.dataset_id, request.dataset_version):
        raise HTTPException(422, 'Primary dataset must match the first selection')
    versions = [service.dataset_version(x_project_id, s.dataset_id, s.dataset_version) for s in selections]
    version = versions[0]
    from evaluation_backend.scorer_selection import resolve_scorers
    if request.execution_mode == 'guardrail':
        from evaluation_backend.guardrail_runs import resolve_bindings
        guardrail_bindings = resolve_bindings(service, x_project_id, target, environment, selections, versions, request.guardrail_bindings)
    else:
        guardrail_bindings = []
    try:
        bindings = ([{'scorer_ids': [b['scorer_id']], 'scorer_metadata': {'evaluation': {'scorers': {b['scorer_id']: {'version':str(b['scorer_version']), 'execution':b['scorer_execution']}}}}} for b in guardrail_bindings] if guardrail_bindings else [resolve_scorers(selected.dataset_id, content, selection=request.scorer_selection,
                    mode=request.scoring_mode, shared=request.scorer_ids, service=service, project=x_project_id) for selected, content in zip(selections, versions)])
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    dataset_records = []
    for selected, content in zip(selections, versions):
        size = len(content['rows'])
        if selected.upload_id:
            try: size = get_upload_store().claim(x_project_id, selected.upload_id, selected.dataset_id, selected.dataset_version, name, experiment)
            except UploadError as error: raise HTTPException(422, str(error)) from None
        if request.datasets is not None and not size:
            raise HTTPException(422, 'Dataset has no samples: ' + selected.dataset_id)
        dataset_records.append({**selected.model_dump(), 'name': content['name'], 'sample_count': size,
                                **bindings[len(dataset_records)], 'objective': content.get('objective','')})
    from evaluation_backend.aspects import snapshot_scorers
    for d in dataset_records:
        d['scorer_bindings'] = snapshot_scorers(service, x_project_id, d['scorer_ids'], d['scorer_metadata'])
    count = sum(d['sample_count'] for d in dataset_records)
    if (request.datasets is not None and not count) or count > 10000:
        raise HTTPException(422, 'An evaluation requires 1 to 10,000 samples across all datasets')
    from evaluation_backend.capabilities import capabilities
    feature_modes = capabilities()
    context = {'feature_modes': feature_modes, 'id': experiment, 'target': target, 'environment': environment, 'dataset_id': request.dataset_id, 'dataset_version': request.dataset_version,
               'upload_id': request.upload_id, 'content_policy': ContentPolicy.from_env().model_dump(), 'request_hash': digest}
    from zoneinfo import ZoneInfo
    generated_name = target['name'][:150] + ' · ' + datetime.now(ZoneInfo(os.getenv('EVAL_NAME_TIMEZONE', 'Asia/Shanghai'))).strftime('%Y-%m-%d %H:%M:%S')
    context.update(name=request.name.strip() or generated_name, job_name=name,
                   target_id=request.target_id, environment_id=request.environment_id,
                   environment_version=request.environment_version, scorer_ids=request.scorer_ids,
                   scoring_mode=request.scoring_mode, total_samples=count,
                   created_at=datetime.now(timezone.utc).isoformat())
    if request.execution_mode == 'guardrail':
        from evaluation_backend.guardrail_registry import resolve
        context['guardrail_connection'] = resolve(target)
        if os.getenv('EVAL_GUARDRAIL_REGISTRY'):
            from evaluation_backend.guardrail_registry import credential
            credential(context['guardrail_connection'])
    context['evaluation_config_id']=request.target_id
    context['evaluation_config_version']=target['version']
    context['execution_mode'] = request.execution_mode
    context['guardrail_bindings'] = guardrail_bindings
    context['scoring_selections'] = dataset_records
    if request.datasets is not None or request.scorer_selection == 'dataset':
        context['datasets'] = dataset_records
    try: service.storage.put('run-contexts', x_project_id, experiment, context, create=True)
    except Conflict:
        context = service.storage.get('run-contexts', x_project_id, experiment)
        if context['request_hash'] != digest: raise HTTPException(409, 'Idempotency conflict')
    target = context.get('target', target)
    environment = context.get('environment', environment)
    concurrency=environment.get('runner',{}).get('concurrency',1)
    if request.model_id != environment['modelId']:
        raise HTTPException(422, 'Model must match the stored environment version')
    resources = PodResources(cpu_millicores=environment['cpuMillicores'], memory_mib=environment['memoryMiB'])
    # Do not put caller-supplied rows or arbitrary snapshot data in Kubernetes annotations.
    snapshot = {'target': target, 'datasetId': request.dataset_id,
                'dataset': {k: version[k] for k in ('name', 'version', 'publishedAt', 'fields')},
                'environmentId': request.environment_id,
                'environment': {**environment, 'modelId': request.model_id, 'runner': {'id': os.getenv('EVAL_EXECUTION_BACKEND', 'kubernetes'), 'concurrency': concurrency}},
                'scorers': []}
    snapshot['dataset'].update(rows=[], objective='', description='', tags=[], metadata={}, scorerIds=[])
    record = {k: v for k,v in request.model_dump().items() if k not in ('snapshot', 'upload_id')}
    record['feature_modes'] = context.get('feature_modes', feature_modes)
    record.update(name=context.get('name', request.name.strip() or generated_name), snapshot=snapshot, sample_count=count, resources=resources.model_dump())
    record['datasets'] = [{k: v for k, v in d.items() if k not in ('upload_id','scorer_metadata','scorer_bindings')} for d in dataset_records]
    effective_ids = list(dict.fromkeys(s for b in bindings for s in b['scorer_ids']))
    record['scorer_ids'] = effective_ids
    job = build_job(request.dataset_id, request.model_id, effective_ids,
                    namespace=cluster.namespace, image=os.environ['EVAL_WORKER_IMAGE'], mock=request.scoring_mode == 'mock', experiment_id=experiment, resources=resources)
    job['metadata']['name'] = name
    job['metadata']['labels']['project'] = project_label(x_project_id)
    job['metadata']['annotations'] = {PREFIX + 'project': x_project_id, PREFIX + 'record': json.dumps(record), PREFIX + 'request-hash': digest}
    job['spec']['template']['spec']['containers'][0]['env'].extend([
        {'name':'EVAL_JUDGE_MODEL_IDS','value':json.dumps(sorted({c['execution']['model_id'] for b in bindings for c in b['scorer_metadata'].get('evaluation',{}).get('scorers',{}).values() if c.get('execution',{}).get('engine')=='llm_judge'}))},
        {'name':'EVAL_CONCURRENCY','value':str(concurrency)},
        {'name': 'EVAL_SCORING_ENABLED', 'value': str(context.get('feature_modes', feature_modes)['scoring_enabled']).lower()},
        {'name': 'EVAL_DATASET_VERSION', 'value': str(request.dataset_version)},
        {'name': 'EVAL_PLATFORM', 'value': 'storage'},
        {'name': 'EVAL_PROJECT_ID', 'value': x_project_id},
        {'name': 'EVAL_RUN_INPUT_ID', 'value': experiment},
        {'name': 'EVAL_STORAGE_TOKEN', 'value': issue_worker_token(x_project_id, experiment)},
        {'name': 'EVAL_TARGET_SNAPSHOT', 'value': json.dumps(target)},
        {'name': 'EVAL_GUARDRAIL_ID', 'value': environment['guardrailId']},
        {'name': 'EVAL_ENVIRONMENT_SNAPSHOT', 'value': json.dumps({**environment, 'id': request.environment_id})},
        {'name': 'EVAL_CONTENT_POLICY', 'value': json.dumps(context['content_policy'])}])
    if request.execution_mode == 'guardrail':
        container = job['spec']['template']['spec']['containers'][0]
        container['command'] = ['python', '-m', 'evaluation_backend.proxy_entrypoint', '--', 'python', '-m', 'evaluation_backend.guardrail_worker']
        container['env'].append({'name': 'EVAL_LLM_PROXY_ENABLED', 'value': 'false'})
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
    event('evaluation.job.created', run_id=experiment, job_name=name, project_id=x_project_id)
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

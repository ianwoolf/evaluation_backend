"""Credential-protected maintenance for finished evaluation workloads and reports."""
import hmac
import os
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from evaluation_backend.observability import event
from evaluation_backend.runs import TERMINAL_STATUSES, kube, owned, project_label
from pydantic import BaseModel, ConfigDict, Field

from evaluation_backend.guardrail_registry import REGISTRY_COLLECTION, REGISTRY_ID, Registry, stored_registry
from evaluation_backend.storage import Conflict, NotFound
from evaluation_backend.storage.service import get_service
from evaluation_backend.upload_api import cleanup_run


def authorize_admin(authorization: str = Header(default='')):
    token = os.getenv('EVAL_ADMIN_TOKEN') or os.getenv('EVAL_TRIGGER_TOKEN')
    if not token:
        raise HTTPException(503, 'Evaluation management credentials are not configured')
    if not hmac.compare_digest(authorization, 'Bearer ' + token):
        raise HTTPException(401, 'Invalid management token')


router = APIRouter(prefix='/api/v1/evaluations/admin', dependencies=[Depends(authorize_admin)])


class GuardrailRegistryUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=1, max_length=128)
    registry: Registry


@router.get('/guardrail-registry')
def get_guardrail_registry(x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    value, revision = stored_registry(service, x_project_id)
    return {'registry': value.model_dump(), 'revision': revision}


@router.put('/guardrail-registry')
def save_guardrail_registry(update: GuardrailRegistryUpdate, x_project_id: str = Header(min_length=1),
                            service=Depends(get_service)):
    _, revision = stored_registry(service, x_project_id)
    if revision != update.revision:
        raise Conflict('Guardrail registry changed. Reload before saving.')
    create = revision.startswith('bootstrap:')
    value = update.registry.model_dump()
    record = service.storage.put(REGISTRY_COLLECTION, x_project_id, REGISTRY_ID, {'registry': value},
                                 create=create, revision=None if create else revision)
    event('evaluation.admin.guardrail_registry.saved', project_id=x_project_id)
    return {'registry': value, 'revision': record['_revision']}


def job_status(job):
    conditions = {c['type'] for c in job.get('status', {}).get('conditions', []) if c.get('status') == 'True'}
    if 'Complete' in conditions:
        return 'COMPLETED'
    if 'Failed' in conditions:
        return 'FAILED'
    return 'RUNNING'


def pod_job(pod):
    return next((ref for ref in pod['metadata'].get('ownerReferences', [])
                 if ref.get('kind') == 'Job' and ref.get('controller')), {})


def pod_deletable(pod, jobs):
    owner = pod_job(pod)
    job = jobs.get(owner.get('name'))
    return (pod.get('status', {}).get('phase') in ('Succeeded', 'Failed')
            and not pod['metadata'].get('deletionTimestamp')
            and (not job or job['metadata']['uid'] != owner.get('uid') or job_status(job) in TERMINAL_STATUSES))


@router.get('/cleanup')
def inventory(x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
              status: Literal['RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'] | None = None,
              sort: Literal['newest', 'oldest'] = 'newest', q: str = Query('', max_length=200),
              cluster=Depends(kube), service=Depends(get_service)):
    selector = 'app=evaluation-worker,project=' + project_label(x_project_id)
    workloads = cluster.call('GET', cluster.jobs, params={'labelSelector': selector}).get('items', [])
    jobs = {}
    for workload in workloads:
        metadata = owned(workload, x_project_id)['metadata']
        jobs[metadata['name']] = {'id': metadata['name'], 'status': job_status(workload),
                                  'created_at': metadata.get('creationTimestamp')}
    pods = []
    if cluster.namespace != 'docker':
        path = f'/api/v1/namespaces/{cluster.namespace}/pods'
        actual_pods = cluster.call('GET', path, params={'labelSelector': selector}).get('items', [])
        jobs_by_name = {job['metadata']['name']: job for job in workloads}
        for pod in actual_pods:
            metadata = pod['metadata']
            pods.append({'id': metadata['name'], 'status': pod.get('status', {}).get('phase', 'Unknown'),
                         'job_id': pod_job(pod).get('name'), 'created_at': metadata.get('creationTimestamp'),
                         'deletable': pod_deletable(pod, jobs_by_name)})
    reports = []
    terms = q.casefold().split()
    filters = {'status': status} if status else None
    for cursor in range(0, 10000, 100):
        page = service.storage.search('experiments', x_project_id, filters=filters, offset=cursor, limit=100)
        for record in page['items']:
            ident = record['id']
            name = record.get('name') or ident
            job_name = record.get('job_name') or 'eval-' + ident.removeprefix('exp-')[:24]
            searchable = ' '.join((name, ident, job_name)).casefold()
            if not all(term in searchable for term in terms):
                continue
            reports.append({'id': ident, 'name': name, 'status': record.get('status', 'RUNNING'),
                            'job_id': job_name, 'job_present': job_name in jobs,
                            'created_at': record.get('created_at')})
        if not page['items'] or cursor + len(page['items']) >= page['total']:
            break

    def sort_key(report):
        try:
            created = datetime.fromisoformat(report['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            time = created.timestamp()
            return (False, -time if sort == 'newest' else time, report['id'])
        except (TypeError, ValueError):
            return (True, 0, report['id'])

    reports.sort(key=sort_key)
    return {'jobs': sorted(jobs.values(), key=lambda job: job['created_at'] or '', reverse=True),
            'pods': pods, 'reports': reports[offset:offset + 100], 'total': len(reports),
            'jobs_supported': cluster.namespace != 'docker'}


@router.delete('/pods/{name}')
def delete_pod(name: str, x_project_id: str = Header(min_length=1), cluster=Depends(kube)):
    if cluster.namespace == 'docker' or not re.fullmatch(r'eval-[a-z0-9-]{1,100}', name):
        raise HTTPException(404, 'Evaluation Pod not found')
    path = f'/api/v1/namespaces/{cluster.namespace}/pods/{name}'
    pod = cluster.call('GET', path)
    metadata = pod['metadata']
    labels = metadata.get('labels', {})
    if labels.get('app') != 'evaluation-worker' or labels.get('project') != project_label(x_project_id):
        raise HTTPException(404, 'Evaluation Pod not found')
    jobs = {}
    owner = pod_job(pod)
    if owner:
        try:
            job = cluster.call('GET', cluster.jobs + '/' + owner['name'])
        except HTTPException as error:
            if error.status_code != 404:
                raise
        else:
            jobs[owner['name']] = job
    if not pod_deletable(pod, jobs):
        raise HTTPException(409, 'Only finished Pods whose Job has finished or been removed can be deleted')
    cluster.call('DELETE', path, json={'preconditions': {'uid': metadata['uid'],
        'resourceVersion': metadata['resourceVersion']}})
    event('evaluation.admin.pod.deleted', project_id=x_project_id, run_id=labels.get('experiment_id'))
    return {'id': name}


@router.delete('/jobs/{name}')
def delete_job(name: str, x_project_id: str = Header(min_length=1),
               cluster=Depends(kube), service=Depends(get_service)):
    if not re.fullmatch(r'eval-[a-f0-9]{24}', name):
        raise HTTPException(404, 'Evaluation Job not found')
    if cluster.namespace == 'docker':
        raise HTTPException(409, 'Job deletion requires the Kubernetes runtime')
    path = cluster.jobs + '/' + name
    job = owned(cluster.call('GET', path), x_project_id)
    status = job_status(job)
    if status not in TERMINAL_STATUSES:
        raise HTTPException(409, 'Only finished Jobs can be deleted')
    metadata = job['metadata']
    ident = metadata['labels']['experiment_id']
    try:
        record = service.storage.get('run-contexts', x_project_id, ident)
    except NotFound:
        record = None
    if record and record.get('status') not in TERMINAL_STATUSES:
        service.storage.put('experiments', x_project_id, ident, {'id': ident, 'status': status})
    cleanup_run(service, x_project_id, ident)
    cluster.call('DELETE', path, json={'propagationPolicy': 'Foreground',
        'preconditions': {'uid': metadata['uid'], 'resourceVersion': metadata['resourceVersion']}})
    event('evaluation.admin.job.deleted', project_id=x_project_id, job_name=name, run_id=ident)
    return {'id': name}


@router.delete('/reports/{ident}')
def delete_report(ident: str, x_project_id: str = Header(min_length=1),
                  cluster=Depends(kube), service=Depends(get_service)):
    if not re.fullmatch(r'exp-[a-f0-9]{32}', ident):
        raise HTTPException(404, 'Evaluation report not found')
    record = service.storage.get('experiments', x_project_id, ident)
    if record.get('status') not in TERMINAL_STATUSES:
        raise HTTPException(409, 'Only reports with a finished status can be deleted')
    job_name = record.get('job_name') or 'eval-' + ident[4:28]
    try:
        cluster.call('GET', cluster.jobs + '/' + job_name)
    except HTTPException as error:
        if error.status_code != 404:
            raise
    else:
        raise HTTPException(409, 'Delete the finished Job before deleting its report')
    delete_run = getattr(service.storage, 'delete_run', None)
    if delete_run is None:
        raise HTTPException(501, 'The storage adapter does not support report deletion')
    cleanup_run(service, x_project_id, ident)
    result = delete_run(x_project_id, ident)
    event('evaluation.admin.report.deleted', project_id=x_project_id, run_id=ident)
    return {'id': ident, **result}

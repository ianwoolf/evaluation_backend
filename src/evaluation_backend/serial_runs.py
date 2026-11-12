"""Release queued comparison jobs once every earlier member has stopped."""
import json
from fastapi import HTTPException
from evaluation_backend.observability import event
from evaluation_backend.runs import WAIT_FOR, kube


def reconcile_serial_runs(cluster=None):
    cluster = cluster if cluster is not None else kube()
    items = cluster.call('GET', cluster.jobs, params={'labelSelector': 'app=evaluation-worker'})['items']
    jobs = {job['metadata']['name']: job for job in items}

    def stopped(name):
        previous = jobs.get(name)
        if previous:
            if WAIT_FOR in previous['metadata'].get('annotations', {}):
                return False
            conditions = previous.get('status', {}).get('conditions', [])
            terminal = any(c['type'] in ('Complete', 'Failed') and c.get('status') == 'True' for c in conditions)
            if not terminal and not previous['spec'].get('suspend'):
                return False
        if cluster.namespace == 'docker':
            return True
        pods = cluster.call('GET', f'/api/v1/namespaces/{cluster.namespace}/pods',
                            params={'labelSelector': 'job-name=' + name})['items']
        return all(pod.get('status', {}).get('phase') in ('Succeeded', 'Failed') for pod in pods)

    for job in items:
        meta = job['metadata']
        wait_for = meta.get('annotations', {}).get(WAIT_FOR)
        if not wait_for or not job['spec'].get('suspend') or meta.get('deletionTimestamp'):
            continue
        try:
            if not all(stopped(name) for name in json.loads(wait_for)):
                continue
            cluster.call('PATCH', cluster.jobs + '/' + meta['name'],
                         content_type='application/merge-patch+json',
                         json={'metadata': {'resourceVersion': meta.get('resourceVersion'), 'annotations': {WAIT_FOR: None}},
                               'spec': {'suspend': False}})
            event('evaluation.job.released', job_name=meta['name'])
        except HTTPException as error:
            if error.status_code not in (404, 409):
                event('evaluation.job.release_failed', level='ERROR', job_name=meta['name'], error=error)

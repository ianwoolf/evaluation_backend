"""Render or submit a direct Kubernetes evaluation Job (not an OpenShell sandbox)."""
import argparse
import json
import yaml
import re
import subprocess
import uuid
from pydantic import BaseModel, ConfigDict, Field


class PodResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cpu_millicores: int = Field(default=2000, ge=1, le=32000, strict=True)
    memory_mib: int = Field(default=2048, ge=128, le=65536, strict=True)

    def kubernetes(self):
        values = {"cpu": f"{self.cpu_millicores}m", "memory": f"{self.memory_mib}Mi"}
        return {"requests": dict(values), "limits": dict(values)}


def build_job(dataset_id, model_id, scorer_ids, *, namespace='evaluation', image='evaluation-worker:experiment', mock=False, experiment_id=None, resources: PodResources | None = None):
    if not dataset_id or not model_id or not scorer_ids:
        raise ValueError('dataset, model and scorer IDs are required')
    if not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', namespace) or len(namespace) > 63:
        raise ValueError('invalid namespace')
    ident = experiment_id or 'exp-' + uuid.uuid4().hex
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', ident):
        raise ValueError('experiment ID must be a valid Kubernetes label')
    if mock and scorer_ids != ['mock-score']:
        raise ValueError('mock mode requires only mock-score')
    labels = {'app': 'evaluation-worker', 'experiment_id': ident}
    values = {'EXPERIMENT_ID': ident, 'EVALUATE_ID': ident, 'EVALUATION_ID': ident,
              'DATASET_ID': dataset_id, 'EVAL_MODEL_ID': model_id, 'SCORER_IDS': json.dumps(scorer_ids),
              'EVAL_TARGET_MODE': 'llm', 'EVAL_SCORER_MODE': 'mock' if mock else 'installed'}
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': 'eval-' + uuid.uuid4().hex[:20], 'namespace': namespace, 'labels': labels},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 600, 'ttlSecondsAfterFinished': 86400,
                     'template': {'metadata': {'labels': labels}, 'spec': {
                         'serviceAccountName': 'evaluation-worker', 'automountServiceAccountToken': False,
                         'restartPolicy': 'Never',
                         'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
                         'containers': [{'name': 'worker', 'image': image, 'imagePullPolicy': 'IfNotPresent',
                             'command': ['python', '-m', 'evaluation_backend.proxy_entrypoint', '--', 'python', '-m', 'evaluation_backend.worker'],
                             'envFrom': [{'configMapRef': {'name': 'evaluation-runtime'}},
                                         {'configMapRef': {'name': 'evaluation-storage'}},
                                         {'configMapRef': {'name': 'evaluation-observability', 'optional': True}}],
                             'env': [{'name': k, 'value': v} for k, v in values.items()] + [
                                 {'name': key, 'valueFrom': {'secretKeyRef': {'name':'evaluation-credentials','key':key,
                                     'optional':key != 'EVAL_MODEL_RESOLVER_TOKEN'}}}
                                 for key in ('EVAL_MODEL_RESOLVER_TOKEN','EVAL_ES_PASSWORD','EVAL_ES_API_KEY','OTEL_EXPORTER_OTLP_LOGS_HEADERS')],
                             'resources': (resources or PodResources()).kubernetes(),
                             'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                             'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'}, {'name': 'home', 'mountPath': '/home/worker'}]}],
                         'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '128Mi'}}, {'name': 'home', 'emptyDir': {'sizeLimit': '128Mi'}}]}}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-id', required=True)
    parser.add_argument('--model-id', required=True)
    parser.add_argument('--scorer-id', action='append', default=[])
    parser.add_argument('--mock', action='store_true')
    parser.add_argument('--namespace', default='evaluation')
    parser.add_argument('--image', default='evaluation-worker:experiment')
    parser.add_argument('--context', default='docker-desktop')
    parser.add_argument('--submit', action='store_true', help='create a Job; otherwise only render YAML')
    args = parser.parse_args()
    if args.mock and args.scorer_id:
        parser.error('--mock cannot be combined with --scorer-id')
    job = build_job(args.dataset_id, args.model_id, ['mock-score'] if args.mock else args.scorer_id,
                    namespace=args.namespace, image=args.image, mock=args.mock)
    if args.submit:
        subprocess.run(['kubectl', '--context', args.context, 'create', '-f', '-'], input=yaml.safe_dump(job, sort_keys=False), text=True, check=True)
        print(json.dumps({'job': job['metadata']['name'], 'experiment_id': job['metadata']['labels']['experiment_id']}))
    else:
        print(yaml.safe_dump(job, sort_keys=False), end='')


if __name__ == '__main__':
    main()

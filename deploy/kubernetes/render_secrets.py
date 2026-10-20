"""Render local ignored credentials as Kubernetes Secrets. Pipe directly to kubectl."""
import argparse
import json
import yaml
from pathlib import Path
from evaluation_backend.config import load_config
import os

parser = argparse.ArgumentParser()
parser.add_argument('--config', default='.env')
parser.add_argument('--namespace', default='evaluation')
args = parser.parse_args()
load_config(args.config)
keys = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'EVAL_MODEL_RESOLVER_TOKEN')
if any(not os.getenv(k) for k in keys):
    raise SystemExit('Missing credentials in selected configuration')
models = Path(os.environ['EVAL_MODEL_REGISTRY']).read_text()
if not isinstance(json.loads(models), dict):
    raise SystemExit('Invalid model registry')
items = [
    {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'evaluation-models', 'namespace': args.namespace},
     'type': 'Opaque', 'stringData': {'models.json': models, 'resolver-token': os.environ['EVAL_MODEL_RESOLVER_TOKEN']}},
    {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'evaluation-runner', 'namespace': args.namespace},
     'type': 'Opaque', 'stringData': {k: os.environ[k] for k in keys}},
]
print(yaml.safe_dump_all(items, sort_keys=False, allow_unicode=True), end='')

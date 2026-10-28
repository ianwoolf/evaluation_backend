"""Render ignored local credentials. Pipe stdout directly to kubectl; do not log."""
import argparse
import json
import os
import sys
from pathlib import Path
import yaml
from evaluation_backend.config import load_config

parser=argparse.ArgumentParser()
parser.add_argument('--config',default='.env')
parser.add_argument('--namespace',default='evaluation')
args=parser.parse_args()
if sys.stdout.isatty():
    parser.error('Pipe output directly to kubectl; credentials must not be printed to the terminal')
load_config(args.config)
keys=('EVAL_MODEL_RESOLVER_TOKEN','EVAL_TRIGGER_TOKEN','EVAL_MODEL_REGISTRY')
if any(not os.getenv(k) for k in keys):
    raise SystemExit('Set EVAL_MODEL_REGISTRY, EVAL_MODEL_RESOLVER_TOKEN and EVAL_TRIGGER_TOKEN in the selected local config')
from evaluation_backend.deployment_config import split_models
models,tokens=split_models(json.loads(Path(os.environ['EVAL_MODEL_REGISTRY']).read_text()))
for record in models.values():
    key=record['token_env']
    if not tokens.get(key) and os.getenv(key):tokens[key]=os.environ[key]
def secret(name,data):return {'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':args.namespace},'type':'Opaque','stringData':data}
credentials={'EVAL_TRIGGER_TOKEN':os.environ['EVAL_TRIGGER_TOKEN'],
             'EVAL_MODEL_RESOLVER_TOKEN':os.environ['EVAL_MODEL_RESOLVER_TOKEN'],**tokens}
for key in ('EVAL_ES_PASSWORD','EVAL_ES_API_KEY','OTEL_EXPORTER_OTLP_LOGS_HEADERS'):
    if os.getenv(key):credentials[key]=os.environ[key]
items=[{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'evaluation-model-registry','namespace':args.namespace},
        'data':{'models.json':json.dumps(models)}},
       secret('evaluation-credentials',credentials)]
print(yaml.safe_dump_all(items,sort_keys=False),end='')

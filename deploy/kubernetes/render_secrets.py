"""Render ignored local credentials. Pipe stdout directly to kubectl; do not log."""
import argparse
import json
import os
from pathlib import Path
import yaml
from evaluation_backend.config import load_config

parser=argparse.ArgumentParser()
parser.add_argument('--config',default='.env')
parser.add_argument('--namespace',default='evaluation')
args=parser.parse_args()
load_config(args.config)
keys=('EVAL_MODEL_RESOLVER_TOKEN','EVAL_TRIGGER_TOKEN','EVAL_MODEL_REGISTRY')
if any(not os.getenv(k) for k in keys):
    raise SystemExit('Set EVAL_MODEL_REGISTRY, EVAL_MODEL_RESOLVER_TOKEN and EVAL_TRIGGER_TOKEN in the selected local config')
models=Path(os.environ['EVAL_MODEL_REGISTRY']).read_text()
if not isinstance(json.loads(models),dict):raise SystemExit('Invalid model registry')
def secret(name,data):return {'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':args.namespace},'type':'Opaque','stringData':data}
items=[secret('evaluation-models',{'models.json':models,'resolver-token':os.environ['EVAL_MODEL_RESOLVER_TOKEN']}),
       secret('evaluation-trigger',{'token':os.environ['EVAL_TRIGGER_TOKEN']}),
       secret('evaluation-runner',{'EVAL_MODEL_RESOLVER_TOKEN':os.environ['EVAL_MODEL_RESOLVER_TOKEN']})]
print(yaml.safe_dump_all(items,sort_keys=False),end='')

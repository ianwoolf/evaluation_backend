"""Server-owned target catalog and trusted run telemetry metadata."""
import json
import os
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Target(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal['GUARDRAIL'] = 'GUARDRAIL'
    provider: Literal['f5', 'nemo guardrail']
    version: int = Field(ge=1, strict=True)
    definition: str = Field(default='', max_length=4000)


def targets():
    default = [{'id':'guardrail-privacy','name':'Privacy & prompt injection','kind':'GUARDRAIL',
        'provider':'nemo guardrail','version':2,'definition':'Block private-data requests and instruction override attempts.'}]
    values = json.loads(os.environ['EVAL_TARGETS']) if os.getenv('EVAL_TARGETS') else default
    result = [Target.model_validate(v).model_dump() for v in values]
    if len({v['id'] for v in result}) != len(result): raise ValueError('Duplicate target IDs')
    return result


def target_metadata(value):
    target = Target.model_validate(value).model_dump()
    return {k:target[k] for k in ('id','name','kind','provider','version')}


def env_target(env=None):
    env = os.environ if env is None else env
    raw = env.get('EVAL_TARGET_SNAPSHOT')
    return target_metadata(json.loads(raw)) if raw else None


def trace_target(service, project, evaluation):
    from evaluation_backend.storage import NotFound
    if os.getenv('EVAL_COLLECTOR_LOCAL') == 'true':
        value = env_target()
    else:
        try: value = service.storage.get('run-contexts', project, evaluation).get('target')
        except NotFound: value = None
    return {'target': target_metadata(value)} if value else {}

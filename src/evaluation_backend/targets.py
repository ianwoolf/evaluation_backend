"""Server-owned target catalog and trusted run telemetry metadata."""
import json
import os
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from evaluation_backend.runs import authorize
from evaluation_backend.storage import Conflict, NotFound
from evaluation_backend.storage.service import get_service, public
from evaluation_backend.observability import observed


class Target(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    name: str = Field(min_length=1, max_length=200)
    kind: Literal['GUARDRAIL'] = 'GUARDRAIL'
    provider: Literal['f5', 'nemo guardrail']
    version: int = Field(ge=1, strict=True)
    definition: str = Field(default='', max_length=4000)


def get_target(service, project, ident):
    value = service.storage.get('targets', project, ident)
    return Target.model_validate({k:value[k] for k in Target.model_fields if k in value}).model_dump()


class SaveTarget(Target):
    revision: str | None = None


router = APIRouter(prefix='/api/v1/evaluations', dependencies=[Depends(authorize)])


@router.get('/targets')
def target_catalog(x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
                   limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('targets', x_project_id, offset=offset, limit=limit)


@router.get('/targets/{ident}')
def read_target(ident: str, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return public(service.storage.get('targets', x_project_id, ident))


@router.post('/targets/{ident}')
@observed('target.save', project_id='x_project_id', target_id='ident')
def save_target(ident: str, value: SaveTarget, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    if ident != value.id:
        raise HTTPException(422, 'Target ID must match the URL')
    try:
        current = service.storage.get('targets', x_project_id, ident)
    except NotFound:
        current = None
    if (current and value.revision != current['_revision']) or (not current and value.revision is not None):
        raise Conflict('Target changed; reload before saving')
    if current and value.version <= current['version']:
        raise Conflict('Target version must increase when updating')
    return public(service.storage.put('targets', x_project_id, ident, value.model_dump(exclude={'revision'}),
        create=current is None, revision=value.revision))


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

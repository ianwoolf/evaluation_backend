"""Project-scoped, immutable environment versions stored in the evaluation database."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from evaluation_backend.runs import authorize
from evaluation_backend.storage import Conflict, NotFound
from evaluation_backend.storage.service import get_service, now, public


class RunnerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(default='kubernetes', min_length=1, max_length=100)
    concurrency: int = Field(default=1, ge=1, le=32, strict=True)


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=4000)
    type: Literal['SANDBOX'] = 'SANDBOX'
    modelId: str = Field(min_length=1, max_length=128)
    guardrailId: str = Field(min_length=1, max_length=128)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    cpuMillicores: int = Field(default=2000, ge=1, le=32000, strict=True)
    memoryMiB: int = Field(default=2048, ge=128, le=65536, strict=True)
    timeoutSeconds: int = Field(default=120, ge=1, le=3600, strict=True)
    network: list[str] = Field(default_factory=list, max_length=100)
    readOnlyPaths: list[str] = Field(default_factory=list, max_length=100)
    writablePaths: list[str] = Field(default_factory=list, max_length=100)
    commands: list[str] = Field(default_factory=list, max_length=100)
    interceptLlm: Literal[True] = True
    interceptGuardrail: Literal[True] = True


class SaveEnvironment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str | None = None
    config: EnvironmentConfig


class PatchResources(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(min_length=1)
    cpuMillicores: int | None = Field(default=None, ge=1, le=32000, strict=True)
    memoryMiB: int | None = Field(default=None, ge=128, le=65536, strict=True)


def environment_version(service, project, ident, version):
    value = service.storage.get('environments', project, ident)
    for item in value['versions']:
        if item['version'] == version:
            return item
    raise NotFound('Environment version not found')


def save_environment(service, project, ident, value):
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', ident):
        raise HTTPException(422, 'Invalid environment ID')
    try: current = service.storage.get('environments', project, ident)
    except NotFound:
        if value.revision is not None: raise Conflict('Environment no longer exists')
        current = None
    if current and value.revision != current['_revision']:
        raise Conflict('Environment changed; reload before saving')
    versions = current['versions'] if current else []
    version = {**value.config.model_dump(), 'version': len(versions) + 1, 'publishedAt': now()}
    record = {'id': ident, 'versions': [*versions, version]}
    return public(service.storage.put('environments', project, ident, record,
        create=current is None, revision=current['_revision'] if current else None))


router = APIRouter(prefix='/api/v1/evaluations', dependencies=[Depends(authorize)])


@router.get('/environments')
def list_environments(x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900), limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('environments', x_project_id, offset=offset, limit=limit)


@router.get('/environments/{ident}')
def get_environment(ident: str, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return public(service.storage.get('environments', x_project_id, ident))


@router.get('/environments/{ident}/versions/{version}')
def get_version(ident: str, version: int, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return environment_version(service, x_project_id, ident, version)


@router.post('/environments/{ident}')
def save(ident: str, value: SaveEnvironment, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return save_environment(service, x_project_id, ident, value)


@router.patch('/environments/{ident}')
def patch(ident: str, value: PatchResources, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    updates = value.model_dump(exclude={'revision'}, exclude_none=True)
    if not updates: raise HTTPException(422, 'Provide cpuMillicores or memoryMiB')
    current = service.storage.get('environments', x_project_id, ident)
    config = {k:v for k,v in current['versions'][-1].items() if k not in ('version', 'publishedAt')}
    return save_environment(service, x_project_id, ident,
        SaveEnvironment(revision=value.revision, config=EnvironmentConfig.model_validate({**config, **updates})))

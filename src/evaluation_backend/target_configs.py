"""Reusable target configuration; temporary sample content is never stored here."""
from evaluation_backend.observability import observed
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from evaluation_backend.runs import authorize, start, StartRun, DatasetSelection, kube
from evaluation_backend.environments import environment_version
from evaluation_backend.targets import get_target
from evaluation_backend.storage import NotFound, Conflict
from evaluation_backend.storage.service import get_service, public, now

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])


class ConfigDataset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: int = Field(ge=1, strict=True)


def configured_datasets(config):
    return config.get('datasets') or [{'dataset_id': config['dataset_id'], 'dataset_version': config['dataset_version']}]


class TargetConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: str | None = None
    dataset_id: str = Field(min_length=1,max_length=128)
    dataset_version: int = Field(ge=1,strict=True)
    environment_id: str = Field(min_length=1,max_length=128)
    environment_version: int = Field(ge=1,strict=True)
    scoring_mode: Literal['mock','installed'] = 'mock'
    scorer_ids: list[str] = Field(default_factory=list,max_length=100)
    scorer_selection: Literal['shared','dataset'] = 'shared'
    data_source: Literal['published','upload'] = 'published'
    datasets: list[ConfigDataset] | None = Field(default=None, min_length=1, max_length=20)


def selection(project, ident, value, service):
    get_target(service, project, ident)
    if service.is_archived(project,'targets',ident):raise HTTPException(422,'Target is archived')
    if service.is_archived(project,'environments',value.environment_id):raise HTTPException(422,'Environment is archived; select an active environment')
    selected = configured_datasets(value.model_dump())
    if len({d['dataset_id'] for d in selected}) != len(selected):
        raise HTTPException(422, 'Select each dataset only once')
    if (value.dataset_id, value.dataset_version) != (selected[0]['dataset_id'], selected[0]['dataset_version']):
        raise HTTPException(422, 'Primary dataset must match the first configured dataset')
    datasets = [service.dataset_version(project,d['dataset_id'],d['dataset_version']) for d in selected]
    environment = environment_version(service,project,value.environment_id,value.environment_version)
    from evaluation_backend.scorer_selection import resolve_scorers
    try:
        for ref, dataset in zip(selected, datasets):
            resolve_scorers(ref['dataset_id'], dataset, selection=value.scorer_selection,
                            mode=value.scoring_mode, shared=value.scorer_ids, service=service, project=project)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return environment


@router.get('/targets/{ident}/config')
def get_config(ident:str,x_project_id:str=Header(min_length=1),service=Depends(get_service)):
    config = public(service.storage.get('target-configs',x_project_id,ident))
    return {**config, 'datasets': configured_datasets(config)}


@router.post('/targets/{ident}/config')
@observed('target_config.save', project_id='x_project_id', target_id='ident')
def save_config(ident:str,value:TargetConfig,x_project_id:str=Header(min_length=1),service=Depends(get_service)):
    environment=selection(x_project_id,ident,value,service)
    try:current=service.storage.get('target-configs',x_project_id,ident)
    except NotFound:current=None
    if (current and current['_revision']!=value.revision) or (not current and value.revision is not None):raise Conflict('Target config changed; reload before saving')
    record={**value.model_dump(exclude={'revision'}),'id':ident,'model_id':environment['modelId'],'updated_at':now()}
    record['datasets'] = configured_datasets(record)
    return public(service.storage.put('target-configs',x_project_id,ident,record,create=current is None,revision=value.revision))


class TriggerTarget(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str = Field(default='',max_length=200)
    config_revision: str = Field(min_length=1,max_length=128)
    upload_id: str | None = Field(default=None,pattern=r'^upload-[a-f0-9]{32}$')
    datasets: list[DatasetSelection] | None = Field(default=None, min_length=1, max_length=20)


@router.post('/targets/{ident}/runs')
@observed('target.trigger', project_id='x_project_id', target_id='ident')
def trigger(ident:str,value:TriggerTarget,x_project_id:str=Header(min_length=1),idempotency_key:str=Header(min_length=16,max_length=128),service=Depends(get_service),cluster=Depends(kube)):
    config=service.storage.get('target-configs',x_project_id,ident)
    if config['_revision']!=value.config_revision:raise Conflict('Target config changed; reload before triggering')
    selections = value.datasets or [DatasetSelection(dataset_id=config['dataset_id'], dataset_version=config['dataset_version'], upload_id=value.upload_id)]
    allowed = {(d['dataset_id'], d['dataset_version']) for d in configured_datasets(config)}
    if any((d.dataset_id, d.dataset_version) not in allowed for d in selections):
        raise HTTPException(422, 'Dataset and version must be selected in Target configuration before triggering')
    if value.datasets is not None and value.upload_id:
        raise HTTPException(422, 'Specify uploads per dataset')
    if any((config['data_source']=='upload') != bool(s.upload_id) for s in selections):
        raise HTTPException(422,'Each dataset requires an upload only in upload mode')
    request=StartRun(name=value.name,target_id=ident,dataset_id=selections[0].dataset_id,dataset_version=selections[0].dataset_version,
        environment_id=config['environment_id'],environment_version=config['environment_version'],model_id=config['model_id'],
        scorer_selection=config.get('scorer_selection','shared'),scorer_ids=config['scorer_ids'],scoring_mode=config['scoring_mode'],snapshot={},upload_id=None if config.get('scorer_selection')=='dataset' else value.upload_id,datasets=selections if config.get('scorer_selection')=='dataset' else value.datasets)
    return start(request,x_project_id,idempotency_key,cluster,service)

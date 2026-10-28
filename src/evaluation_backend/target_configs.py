"""Reusable target configuration; temporary sample content is never stored here."""
from evaluation_backend.observability import observed
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from evaluation_backend.runs import authorize, start, StartRun, kube
from evaluation_backend.environments import environment_version
from evaluation_backend.targets import get_target
from evaluation_backend.storage import NotFound, Conflict
from evaluation_backend.storage.service import get_service, public, now

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])


class TargetConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: str | None = None
    dataset_id: str = Field(min_length=1,max_length=128)
    dataset_version: int = Field(ge=1,strict=True)
    environment_id: str = Field(min_length=1,max_length=128)
    environment_version: int = Field(ge=1,strict=True)
    scoring_mode: Literal['mock','installed'] = 'mock'
    scorer_ids: list[str] = Field(min_length=1,max_length=100)
    data_source: Literal['published','upload'] = 'published'


def selection(project, ident, value, service):
    get_target(service, project, ident)
    dataset=service.dataset_version(project,value.dataset_id,value.dataset_version)
    environment = environment_version(service,project,value.environment_id,value.environment_version)
    if value.scoring_mode=='mock':
        if value.scorer_ids!=['mock-score']:raise HTTPException(422,'Mock mode requires only mock-score')
    else:
        from evaluation_backend.experiment import Dataset
        from evaluation_backend.scorers import InstalledScorerLibrary
        try:
            if len(value.scorer_ids)!=len(set(value.scorer_ids)):raise ValueError('Duplicate scorer IDs')
            if dataset.get('scorerIds') and any(s not in dataset['scorerIds'] for s in value.scorer_ids):raise ValueError('Scorer is not enabled for this dataset version')
            InstalledScorerLibrary().get_scorers(Dataset(value.dataset_id,dataset['name'],dataset['metadata']),value.scorer_ids)
        except ValueError as e:raise HTTPException(422,str(e)) from None
    return environment


@router.get('/targets/{ident}/config')
def get_config(ident:str,x_project_id:str=Header(min_length=1),service=Depends(get_service)):
    return public(service.storage.get('target-configs',x_project_id,ident))


@router.post('/targets/{ident}/config')
@observed('target_config.save', project_id='x_project_id', target_id='ident')
def save_config(ident:str,value:TargetConfig,x_project_id:str=Header(min_length=1),service=Depends(get_service)):
    environment=selection(x_project_id,ident,value,service)
    try:current=service.storage.get('target-configs',x_project_id,ident)
    except NotFound:current=None
    if (current and current['_revision']!=value.revision) or (not current and value.revision is not None):raise Conflict('Target config changed; reload before saving')
    record={**value.model_dump(exclude={'revision'}),'id':ident,'model_id':environment['modelId'],'updated_at':now()}
    return public(service.storage.put('target-configs',x_project_id,ident,record,create=current is None,revision=value.revision))


class TriggerTarget(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str = Field(default='',max_length=200)
    config_revision: str = Field(min_length=1,max_length=128)
    upload_id: str | None = Field(default=None,pattern=r'^upload-[a-f0-9]{32}$')


@router.post('/targets/{ident}/runs')
@observed('target.trigger', project_id='x_project_id', target_id='ident')
def trigger(ident:str,value:TriggerTarget,x_project_id:str=Header(min_length=1),idempotency_key:str=Header(min_length=16,max_length=128),service=Depends(get_service),cluster=Depends(kube)):
    config=service.storage.get('target-configs',x_project_id,ident)
    if config['_revision']!=value.config_revision:raise Conflict('Target config changed; reload before triggering')
    if (config['data_source']=='upload') != bool(value.upload_id):raise HTTPException(422,'Upload data is required only for upload-mode configurations')
    request=StartRun(name=value.name,target_id=ident,dataset_id=config['dataset_id'],dataset_version=config['dataset_version'],
        environment_id=config['environment_id'],environment_version=config['environment_version'],model_id=config['model_id'],
        scorer_ids=config['scorer_ids'],scoring_mode=config['scoring_mode'],snapshot={},upload_id=value.upload_id)
    return start(request,x_project_id,idempotency_key,cluster,service)

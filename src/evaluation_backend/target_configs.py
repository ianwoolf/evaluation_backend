"""Reusable target configuration; temporary sample content is never stored here."""
from evaluation_backend.guardrail_contracts import ProviderId
from evaluation_backend.observability import observed
from typing import Literal, Annotated
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from evaluation_backend.runs import authorize, start_run, run_job_name, StartRun, DatasetSelection, kube
from evaluation_backend.environments import environment_version
from evaluation_backend.targets import get_target, Target
from evaluation_backend.storage import NotFound, Conflict
from evaluation_backend.storage.service import get_service, public, now

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])


class ConfigDataset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: int = Field(ge=1, strict=True)


def configured_datasets(config):
    return config.get('datasets') or [{'dataset_id': config['dataset_id'], 'dataset_version': config['dataset_version']}]


from evaluation_backend.guardrail_contracts import GuardrailDatasetConfig

class ConfigBinding(GuardrailDatasetConfig):
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: int = Field(ge=1, strict=True)

class TargetConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    execution_mode: Literal['legacy','guardrail'] = 'legacy'
    guardrail_bindings: list[ConfigBinding] | None = Field(default=None, max_length=20)
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


def selection(project, ident, value, service, target=None):
    target = target or get_target(service, project, ident)
    if service.is_archived(project,'targets',ident):raise HTTPException(422,'Target is archived')
    if service.is_archived(project,'environments',value.environment_id):raise HTTPException(422,'Environment is archived; select an active environment')
    selected = configured_datasets(value.model_dump())
    if len({d['dataset_id'] for d in selected}) != len(selected):
        raise HTTPException(422, 'Select each dataset only once')
    if (value.dataset_id, value.dataset_version) != (selected[0]['dataset_id'], selected[0]['dataset_version']):
        raise HTTPException(422, 'Primary dataset must match the first configured dataset')
    datasets = [service.dataset_version(project,d['dataset_id'],d['dataset_version']) for d in selected]
    environment = environment_version(service,project,value.environment_id,value.environment_version)
    if value.execution_mode == 'guardrail':
        if value.data_source != 'published' or value.scoring_mode != 'installed': raise HTTPException(422, 'Guardrail evaluation requires published datasets and installed scorers')
        pairs = [(b.dataset_id,b.dataset_version) for b in value.guardrail_bindings or []]
        if len(pairs) != len(set(pairs)) or set(pairs) != {(d['dataset_id'],d['dataset_version']) for d in selected}:
            raise HTTPException(422, 'Configure exactly one scorer/aspect binding for each selected dataset')
        from evaluation_backend.guardrail_runs import resolve_bindings
        resolve_bindings(service, project, target, environment, [DatasetSelection(**d) for d in selected], datasets, [b.model_dump() for b in value.guardrail_bindings or []], validate_connection=False)
        return environment
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
    config = public(load_config(service,x_project_id,ident))
    try:
        environment = environment_version(service, x_project_id, config['environment_id'], config.get('environment_version', 1)) if 'environment_id' in config else {}
    except NotFound:
        environment = {}
    return {**config, 'datasets': configured_datasets(config),
            'concurrency': environment.get('runner', {}).get('concurrency', 1)}


@router.post('/targets/{ident}/config')
@observed('target_config.save', project_id='x_project_id', target_id='ident')
def save_config(ident:str,value:TargetConfig,x_project_id:str=Header(min_length=1),service=Depends(get_service)):
    if service.storage.get('targets',x_project_id,ident).get('evaluation_config'):
        raise Conflict('Use the unified Evaluation Configure form')
    environment=selection(x_project_id,ident,value,service)
    try:current=service.storage.get('target-configs',x_project_id,ident)
    except NotFound:current=None
    if (current and current['_revision']!=value.revision) or (not current and value.revision is not None):raise Conflict('Target config changed; reload before saving')
    record={**value.model_dump(exclude={'revision'}),'id':ident,'model_id':environment['modelId'],'updated_at':now()}
    record['datasets'] = configured_datasets(record)
    return public(service.storage.put('target-configs',x_project_id,ident,record,create=current is None,revision=value.revision))


class TriggerTarget(BaseModel):
    concurrency: int | None = Field(default=None, ge=1, le=32, strict=True)
    concurrency_group: str | None = Field(default=None, pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    provider: ProviderId | None = None
    guardrail_id: str | None = Field(default=None,min_length=1,max_length=128)
    model_config=ConfigDict(extra='forbid')
    name: str = Field(default='',max_length=200)
    config_revision: str = Field(min_length=1,max_length=128)
    upload_id: str | None = Field(default=None,pattern=r'^upload-[a-f0-9]{32}$')
    datasets: list[DatasetSelection] | None = Field(default=None, min_length=1, max_length=20)


@router.post('/targets/{ident}/runs')
@observed('target.trigger', project_id='x_project_id', target_id='ident')
def trigger(ident:str,value:TriggerTarget,x_project_id:str=Header(min_length=1),idempotency_key:str=Header(min_length=16,max_length=128),service=Depends(get_service),cluster=Depends(kube)):
    return trigger_target(ident, value, x_project_id, idempotency_key, service, cluster)


class TriggerComparison(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group_id: str = Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    levels: list[Annotated[int, Field(strict=True, ge=1, le=32)]] = Field(min_length=2, max_length=8)
    config_revision: str = Field(min_length=1, max_length=128)
    provider: ProviderId | None = None
    guardrail_id: str | None = Field(default=None, min_length=1, max_length=128)
    datasets: list[DatasetSelection] | None = Field(default=None, min_length=1, max_length=20)


@router.post('/targets/{ident}/run-comparison')
def trigger_comparison(ident: str, value: TriggerComparison, x_project_id: str = Header(min_length=1),
                       service=Depends(get_service), cluster=Depends(kube)):
    if len(set(value.levels)) != len(value.levels):
        raise HTTPException(422, 'Choose 2–8 distinct concurrency levels from 1 to 32')
    config = load_config(service, x_project_id, ident)
    shared = value.model_dump(exclude={'group_id', 'levels'})
    runs, predecessors = [], []
    connection = None
    for index, level in enumerate(value.levels):
        member_key = f'{value.group_id}:serial:{index}'
        member = TriggerTarget(**shared, concurrency=level, concurrency_group=value.group_id)
        runs.append(trigger_target(ident, member, x_project_id, member_key, service, cluster,
                                   wait_for=predecessors, config=config, serial_levels=value.levels, connection=connection))
        predecessors.append(run_job_name(x_project_id, member_key))
        if index == 0 and config.get('execution_mode') == 'guardrail':
            context = service.storage.get('run-contexts', x_project_id, runs[0]['experiment_id'])
            connection = context['guardrail_connection']
    return {'items': runs}


def trigger_target(ident, value, x_project_id, idempotency_key, service, cluster, *, wait_for=None, config=None, serial_levels=None, connection=None):
    config=config if config is not None else load_config(service,x_project_id,ident)
    if config['_revision']!=value.config_revision:raise Conflict('Target config changed; reload before triggering')
    selections = value.datasets or [DatasetSelection(**d, upload_id=value.upload_id) for d in configured_datasets(config)]
    allowed = {(d['dataset_id'], d['dataset_version']) for d in configured_datasets(config)}
    if config.get('execution_mode')=='guardrail' and (len(selections)!=len(allowed) or {(d.dataset_id,d.dataset_version) for d in selections}!=allowed):
        raise HTTPException(422, 'Every run of this Evaluation version must use all configured datasets')
    if any((d.dataset_id, d.dataset_version) not in allowed for d in selections):
        raise HTTPException(422, 'Dataset and version must be selected in Target configuration before triggering')
    if config.get('execution_mode')=='guardrail':
        uploads = {s.dataset_id: s.upload_id for s in selections}
        selections = [DatasetSelection(**d, upload_id=uploads[d['dataset_id']]) for d in configured_datasets(config)]
    if value.datasets is not None and value.upload_id:
        raise HTTPException(422, 'Specify uploads per dataset')
    if config['data_source'] == 'upload' and any(not s.upload_id for s in selections):
        raise HTTPException(422, 'Each dataset requires an upload in upload mode')
    per_dataset = config.get('execution_mode') == 'guardrail' or config.get('scorer_selection') == 'dataset' or value.datasets is not None
    request=StartRun(concurrency=value.concurrency, concurrency_group=value.concurrency_group, guardrail_provider=value.provider,guardrail_instance_id=value.guardrail_id,execution_mode=config.get("execution_mode","legacy"), config_revision=config["_revision"], guardrail_bindings=config.get("guardrail_bindings"), name=value.name,target_id=ident,dataset_id=selections[0].dataset_id,dataset_version=selections[0].dataset_version,
        environment_id=config['environment_id'],environment_version=config['environment_version'],model_id=config['model_id'],
        scorer_selection=config.get('scorer_selection','shared'),scorer_ids=config['scorer_ids'],scoring_mode=config['scoring_mode'],snapshot={},upload_id=None if per_dataset else value.upload_id,datasets=selections if per_dataset else None)
    return start_run(request,x_project_id,idempotency_key,cluster,service,wait_for=wait_for,
                     serial_levels=serial_levels,connection=connection)


def load_config(service, project, ident):
    try: target = service.storage.get('targets', project, ident)
    except NotFound: target = {}
    if target.get('evaluation_config'):
        return {**target['evaluation_config'], 'id':ident, 'version':target['version'], '_revision':target['_revision']}
    return service.storage.get('target-configs', project, ident)


class EvaluationConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Target
    config: TargetConfig
    revision: str | None = None


@router.post('/targets/{ident}/configuration')
def save_configuration(ident:str, value:EvaluationConfiguration, x_project_id:str=Header(min_length=1), service=Depends(get_service)):
    if value.target.id != ident: raise HTTPException(422, 'Configuration ID must match URL')
    try: current = service.storage.get('targets', x_project_id, ident)
    except NotFound: current = None
    if (current and current['_revision'] != value.revision) or (not current and value.revision is not None):
        raise Conflict('Evaluation changed; reload before saving')
    environment = selection(x_project_id, ident, value.config, service, target=value.target.model_dump())
    config = {**value.config.model_dump(exclude={'revision'}), 'model_id':environment['modelId'], 'updated_at':now()}
    config['datasets'] = configured_datasets(config)
    record = {**value.target.model_dump(), 'version':(current['version']+1 if current else 1), 'evaluation_config':config}
    return public(service.storage.put('targets', x_project_id, ident, record, create=current is None, revision=value.revision))

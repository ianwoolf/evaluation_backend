"""Direct Guardrail run selection and run-scoped mock connection resolution."""
import os
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from evaluation_backend.runs import StartRun, DatasetSelection, start, authorize, kube
from evaluation_backend.run_auth import verify_worker_token
from evaluation_backend.storage.service import get_service, DatasetContent

router = APIRouter(prefix='/api/v1/evaluations')


def resolve_bindings(service, project, target, environment, selections, versions, configured=None, validate_connection=True):
    if target.get('kind') != 'GUARDRAIL':
        raise HTTPException(422, 'Direct Guardrail evaluation requires a GUARDRAIL target')
    from evaluation_backend.guardrail_registry import resolve
    if validate_connection: resolve(target)
    if environment['modelId'] != 'none':
        raise HTTPException(422, 'Direct Guardrail evaluation requires Model: None')
    commit = os.getenv('EVAL_GUARDRAIL_SCORER_COMMIT', '')
    result = []
    for i, (selection, raw) in enumerate(zip(selections, versions)):
        content = DatasetContent.model_validate({k:v for k,v in raw.items() if k not in ('version','publishedAt')})
        if content.evaluation_type != 'guardrail':
            raise HTTPException(422, 'Select only Guardrail datasets for this run')
        if service.is_archived(project, 'datasets', selection.dataset_id):
            raise HTTPException(422, 'Dataset is archived')
        from evaluation_backend.guardrail_contracts import GuardrailDatasetConfig, validate_guardrail_rows
        pinned = next((b for b in (configured or []) if b['dataset_id'] == selection.dataset_id and b['dataset_version'] == selection.dataset_version), None)
        config = GuardrailDatasetConfig.model_validate({k:pinned[k] for k in GuardrailDatasetConfig.model_fields if k in pinned}) if pinned else content.guardrail
        if config is None:
            raise HTTPException(422, 'Configure a scorer and aspect for each dataset in Evaluation')
        try: validate_guardrail_rows(content.fields, content.rows, config)
        except ValueError as error: raise HTTPException(422, str(error)) from None
        if service.is_archived(project, 'scorers', config.scorer_id):
            raise HTTPException(422, 'Scorer is archived')
        doc = service.storage.get('scorers',project,config.scorer_id)
        spec = next((v for v in doc['versions'] if v['version'] == config.scorer_version),None)
        if not doc.get('enabled',True) or spec is None:
            raise HTTPException(422,'Scorer is disabled or version is missing')
        execution = spec.get('execution')
        if execution:
            from evaluation_backend.git_scorers import artifact
            try: pinned, source_url = artifact(execution)
            except ValueError as error: raise HTTPException(422,str(error)) from None
            selected_commit = pinned.commit
        elif config.scorer_id == 'guardrail-git-classifier' and config.scorer_version == 1:
            execution = dict(engine='git',repo_url=spec['repoUrl'],script_path=spec['scriptPath'],commit=commit,timeout=30)
            from evaluation_backend.git_scorers import artifact
            pinned, source_url = artifact(execution)
            selected_commit = pinned.commit
        else:
            raise HTTPException(422,'Configure Git execution for the selected scorer version')
        result.append({'binding_id': f'binding-{i+1:03d}', 'dataset_id': selection.dataset_id,
            'dataset_version': selection.dataset_version, 'sample_count': len(content.rows),
            'scorer_id': config.scorer_id, 'scorer_version': config.scorer_version,
            'aspect_id': config.aspect_id, 'aspect_name': config.aspect_name,
            'scorer_commit': selected_commit, 'scorer_execution': execution,
            'label_mapping': config.label_mapping, 'source': 'mock'})
    return result


class GuardrailRun(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config_revision: str = Field(min_length=1)
    name: str = Field(default='', max_length=200)
    target_id: str
    datasets: list[DatasetSelection] = Field(min_length=1, max_length=20)
    environment_id: str
    environment_version: int = Field(ge=1)


@router.post('/guardrail-runs', dependencies=[Depends(authorize)])
def trigger(value: GuardrailRun, x_project_id: str = Header(min_length=1),
            idempotency_key: str = Header(min_length=16,max_length=128), cluster=Depends(kube),service=Depends(get_service)):
    if any(d.upload_id for d in value.datasets):
        raise HTTPException(422, 'Guardrail runs currently require published datasets')
    from evaluation_backend.target_configs import load_config, TriggerTarget, trigger as configured_trigger
    config = load_config(service, x_project_id, value.target_id)
    if config.get('execution_mode') != 'guardrail' or (config['environment_id'], config['environment_version']) != (value.environment_id, value.environment_version):
        raise HTTPException(422, 'Use the saved Evaluation configuration and environment')
    return configured_trigger(value.target_id, TriggerTarget(name=value.name, config_revision=value.config_revision, datasets=value.datasets), x_project_id, idempotency_key, service, cluster)



@router.get('/guardrail-detail/{evaluation}')
def detail(evaluation: str, authorization: str = Header(default=''),x_project_id: str = Header(min_length=1),service=Depends(get_service)):
    claims = verify_worker_token(authorization.removeprefix('Bearer '))
    if claims['project'] != x_project_id or claims['evaluation'] != evaluation:
        raise HTTPException(403, 'Run capability mismatch')
    context = service.storage.get('run-contexts',x_project_id,evaluation)
    if context.get('execution_mode') != 'guardrail':
        raise HTTPException(403, 'Not a Guardrail run')
    connection=context.get('guardrail_connection')
    if connection is None:
        from evaluation_backend.guardrail_registry import resolve
        connection=resolve(context['target'])
    from evaluation_backend.guardrail_registry import credential
    token=credential(connection)
    return {'endpoint':connection['address'],'token':token,'source':'configured'}


@router.get('/guardrail-reports',dependencies=[Depends(authorize)])
def reports(x_project_id:str=Header(min_length=1),service=Depends(get_service),config_id:str|None=None,config_version:int|None=None):
    from evaluation_backend.storage import NotFound
    items=[];cursor=0
    while cursor<10000:
        page=service.storage.search('run-contexts',x_project_id,offset=cursor,limit=100)
        for context in page['items']:
            if context.get('execution_mode')!='guardrail':continue
            evaluation_id=context.get('evaluation_config_id',context.get('target_id'))
            evaluation_version=context.get('evaluation_config_version',context.get('target',{}).get('version'))
            if config_id is not None and evaluation_id!=config_id:continue
            if config_version is not None and evaluation_version!=config_version:continue
            try:status=service.storage.get('experiments',x_project_id,context['id']).get('status','RUNNING')
            except NotFound:continue
            t=context.get('target',{});c=context.get('guardrail_connection',{})
            items.append({'id':context['id'],'name':context.get('name',context['id']),'status':status,'created_at':context.get('created_at',''),
                          'config_id':evaluation_id,'config_version':evaluation_version,'report_id':context.get('job_name',context['id']),'guardrail_id':c.get('id',t.get('guardrail_id') or t.get('id')),'provider':c.get('provider',t.get('provider'))})
        cursor+=len(page['items'])
        if not page['items'] or cursor>=page['total']:break
    items.sort(key=lambda x:x['created_at'],reverse=True)
    return {'items':items,'total':len(items)}

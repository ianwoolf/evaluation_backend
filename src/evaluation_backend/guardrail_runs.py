"""Direct Guardrail run selection and run-scoped mock connection resolution."""
import os
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from evaluation_backend.runs import StartRun, DatasetSelection, start, authorize, kube
from evaluation_backend.run_auth import verify_worker_token
from evaluation_backend.storage.service import get_service, DatasetContent

router = APIRouter(prefix='/api/v1/evaluations')


def resolve_bindings(service, project, target, environment, selections, versions):
    if target['id'] != os.getenv('EVAL_MOCK_GUARDRAIL_ID', 'mock-guardrail'):
        raise HTTPException(422, 'This target has no direct Guardrail connection configured')
    if environment['modelId'] != 'none':
        raise HTTPException(422, 'Direct Guardrail evaluation requires Model: None')
    commit = os.getenv('EVAL_GUARDRAIL_SCORER_COMMIT', '')
    result = []
    for i, (selection, raw) in enumerate(zip(selections, versions)):
        content = DatasetContent.model_validate({k:v for k,v in raw.items() if k not in ('version','publishedAt')})
        if content.evaluation_type != 'guardrail' or not content.guardrail:
            raise HTTPException(422, 'Select only Guardrail datasets for this run')
        if service.is_archived(project, 'datasets', selection.dataset_id):
            raise HTTPException(422, 'Dataset is archived')
        config = content.guardrail
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
    first = value.datasets[0]
    request = StartRun(execution_mode='guardrail', name=value.name,target_id=value.target_id,
        dataset_id=first.dataset_id,dataset_version=first.dataset_version,datasets=value.datasets,
        environment_id=value.environment_id,environment_version=value.environment_version,
        model_id='none',scorer_ids=['guardrail-git-classifier'],scorer_selection='dataset',scoring_mode='installed',snapshot={})
    return start(request,x_project_id,idempotency_key,cluster,service)


@router.get('/guardrail-detail/{evaluation}')
def detail(evaluation: str, authorization: str = Header(default=''),x_project_id: str = Header(min_length=1),service=Depends(get_service)):
    claims = verify_worker_token(authorization.removeprefix('Bearer '))
    if claims['project'] != x_project_id or claims['evaluation'] != evaluation:
        raise HTTPException(403, 'Run capability mismatch')
    context = service.storage.get('run-contexts',x_project_id,evaluation)
    if context.get('execution_mode') != 'guardrail':
        raise HTTPException(403, 'Not a Guardrail run')
    token = os.getenv('EVAL_MOCK_GUARDRAIL_TOKEN','')
    if not token: raise HTTPException(503,'Guardrail credential unavailable')
    return {'endpoint':os.environ['EVAL_MOCK_GUARDRAIL_URL'],'token':token,'source':'mock'}

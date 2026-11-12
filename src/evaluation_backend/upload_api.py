"""Upload validation and run-scoped input retrieval; raw files never enter ES."""
from fastapi import APIRouter,Depends,Header,Request,HTTPException,Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from evaluation_backend.runs import authorize
from evaluation_backend.storage.service import get_service
from evaluation_backend.uploads import get_upload_store,parse_upload,parse_dataset_xlsx,UploadError,MAX_BYTES

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])


@router.post('/datasets/actions/parse-xlsx')
async def parse_xlsx(request:Request, x_project_id:str=Header(min_length=1)):
    limit = 2 * 1024 * 1024
    data = await request.body()
    if len(data) > limit:
        raise HTTPException(413, 'Excel import limit: 2 MB')
    try:
        records = await run_in_threadpool(parse_dataset_xlsx, data)
        response = JSONResponse({'rows': jsonable_encoder(records)})
    except (UploadError, ValueError) as error:
        message = str(error) if isinstance(error, UploadError) else 'Excel contains an invalid cell value'
        raise HTTPException(422, message) from None
    if len(response.body) > limit:
        raise HTTPException(413, 'Excel records exceed the 2 MB import limit')
    return response


@router.post('/uploads')
async def upload(request:Request, dataset_id:str=Query(min_length=1,max_length=128),dataset_version:int=Query(ge=1),
                 target_id:str|None=Query(default=None,min_length=1,max_length=128),
                 config_revision:str|None=Query(default=None,min_length=1,max_length=128),
                 x_project_id:str=Header(min_length=1),x_upload_filename:str=Header(min_length=1,max_length=600),service=Depends(get_service),store=Depends(get_upload_store)):
    from urllib.parse import unquote
    x_upload_filename = unquote(x_upload_filename)
    schema=await run_in_threadpool(service.dataset_version,x_project_id,dataset_id,dataset_version)
    binding = None
    if target_id is not None:
        from evaluation_backend.target_configs import configured_datasets, load_config
        config = await run_in_threadpool(load_config, service, x_project_id, target_id)
        if config['_revision'] != config_revision:
            raise HTTPException(409, 'Evaluation configuration changed; reload before uploading')
        if {'dataset_id': dataset_id, 'dataset_version': dataset_version} not in configured_datasets(config):
            raise HTTPException(422, 'Upload must match a dataset version pinned to this Evaluation')
        if config.get('execution_mode') == 'guardrail':
            binding = next((b for b in config.get('guardrail_bindings') or []
                            if (b['dataset_id'], b['dataset_version']) == (dataset_id, dataset_version)), None)
            if binding is None:
                binding = schema.get('guardrail')
            if binding is None:
                raise HTTPException(422, 'Configure a Guardrail scorer and label mapping before uploading')
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>MAX_BYTES:raise HTTPException(413,'Upload exceeds 8 MB')
    try:
        rows=await run_in_threadpool(parse_upload,bytes(data),x_upload_filename,schema)
        if binding is not None:
            from evaluation_backend.guardrail_runs import validate_uploaded_rows
            await run_in_threadpool(validate_uploaded_rows, {**schema, 'rows': rows}, binding)
        return await run_in_threadpool(store.create,x_project_id,dataset_id,dataset_version,schema,rows)
    except UploadError as e:raise HTTPException(422,str(e)) from None


@router.get('/run-input/{evaluation}')
def run_input(evaluation:str,x_project_id:str=Header(min_length=1),service=Depends(get_service),store=Depends(get_upload_store)):
    context=service.storage.get('run-contexts',x_project_id,evaluation)
    if context.get('datasets'):
        contents = []
        for selection in context['datasets']:
            if selection.get('upload_id'):
                try: content = store.read(x_project_id, selection['upload_id'], evaluation)
                except UploadError as e: raise HTTPException(410, str(e)) from None
            else:
                content = service.dataset_version(x_project_id, selection['dataset_id'], selection['dataset_version'])
            contents.append({**content, 'dataset_id': selection['dataset_id'], 'version': selection['dataset_version'],
                             **({'scorer_ids': selection['scorer_ids'], 'metadata': selection['scorer_metadata']} if 'scorer_ids' in selection else {})})
        return {'datasets': contents, 'guardrail_bindings': context.get('guardrail_bindings', [])}
    if context.get('upload_id'):
        try:return store.read(x_project_id,context['upload_id'],evaluation)
        except UploadError as e:raise HTTPException(410,str(e)) from None
    return service.dataset_version(x_project_id,context['dataset_id'],context['dataset_version'])


def cleanup_run(service,project,evaluation):
    from evaluation_backend.storage import NotFound
    try:context=service.storage.get('run-contexts',project,evaluation)
    except NotFound:return
    if context.get('concurrency_group'):
        return  # Group uploads expire after all members have had time to fetch the shared input.
    if context.get('upload_id'):get_upload_store().delete(project,context['upload_id'])
    for selection in context.get('datasets', []):
        if selection.get('upload_id'):get_upload_store().delete(project,selection['upload_id'])


def reconcile_uploads():
    from evaluation_backend.runs import kube
    cluster=kube()
    def terminal(name):
        try:job=cluster.call('GET',cluster.jobs+'/'+name)
        except HTTPException as e:
            if e.status_code==404:return None  # allow a grace period for in-flight Job creation
            raise
        return bool(job.get('spec',{}).get('suspend')) or any(c['type'] in ('Complete','Failed') and c.get('status')=='True' for c in job.get('status',{}).get('conditions',[]))
    get_upload_store().reconcile(terminal)

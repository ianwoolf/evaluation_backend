"""Upload validation and run-scoped input retrieval; raw files never enter ES."""
from fastapi import APIRouter,Depends,Header,Request,HTTPException,Query
from evaluation_backend.runs import authorize
from evaluation_backend.storage.service import get_service
from evaluation_backend.uploads import get_upload_store,parse_upload,UploadError,MAX_BYTES

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])


@router.post('/uploads')
async def upload(request:Request, dataset_id:str=Query(min_length=1,max_length=128),dataset_version:int=Query(ge=1),
                 x_project_id:str=Header(min_length=1),x_upload_filename:str=Header(min_length=1,max_length=600),service=Depends(get_service),store=Depends(get_upload_store)):
    from urllib.parse import unquote
    x_upload_filename = unquote(x_upload_filename)
    from starlette.concurrency import run_in_threadpool
    schema=await run_in_threadpool(service.dataset_version,x_project_id,dataset_id,dataset_version)
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>MAX_BYTES:raise HTTPException(413,'Upload exceeds 8 MB')
    try:
        rows=await run_in_threadpool(parse_upload,bytes(data),x_upload_filename,schema)
        return await run_in_threadpool(store.create,x_project_id,dataset_id,dataset_version,schema,rows)
    except UploadError as e:raise HTTPException(422,str(e)) from None


@router.get('/run-input/{evaluation}')
def run_input(evaluation:str,x_project_id:str=Header(min_length=1),service=Depends(get_service),store=Depends(get_upload_store)):
    context=service.storage.get('run-contexts',x_project_id,evaluation)
    if context.get('upload_id'):
        try:return store.read(x_project_id,context['upload_id'],evaluation)
        except UploadError as e:raise HTTPException(410,str(e)) from None
    return service.dataset_version(x_project_id,context['dataset_id'],context['dataset_version'])


def cleanup_run(service,project,evaluation):
    from evaluation_backend.storage import NotFound
    try:context=service.storage.get('run-contexts',project,evaluation)
    except NotFound:return
    if context.get('upload_id'):get_upload_store().delete(project,context['upload_id'])


def reconcile_uploads():
    from evaluation_backend.runs import kube
    cluster=kube()
    if cluster.namespace == 'docker':
        # Enforce deadlines even when no browser is polling a Docker run.
        cluster.call('GET', cluster.jobs)
    def terminal(name):
        try:job=cluster.call('GET',cluster.jobs+'/'+name)
        except HTTPException as e:
            if e.status_code==404:return None  # allow a grace period for in-flight Job creation
            raise
        return bool(job.get('spec',{}).get('suspend')) or any(c['type'] in ('Complete','Failed') and c.get('status')=='True' for c in job.get('status',{}).get('conditions',[]))
    get_upload_store().reconcile(terminal)

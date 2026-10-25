"""Runner-local collector: OTLP/HTTP JSON and typed evaluation feedback to storage.

This is a Python collector service, not the upstream Collector distribution.
OTLP protobuf/gRPC are intentionally not advertised.
"""
import os
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from evaluation_backend.runs import authorize
from evaluation_backend.content_policy import ContentPolicy
from evaluation_backend.targets import trace_target
from evaluation_backend.storage import StorageError, NotFound, Conflict
from evaluation_backend.storage.api import router
from evaluation_backend.storage.service import get_service, now


def create_collector():
    backend = os.getenv('EVAL_COLLECTOR_EXPORTER', 'elasticsearch')
    if backend == 'langsmith':
        raise NotImplementedError('LangSmith collector exporter is not implemented')
    if backend != 'elasticsearch':
        raise ValueError('Unknown collector exporter')
    policy = ContentPolicy.from_env()
    app = FastAPI(title='Runner collector')
    app.include_router(router)

    @app.middleware('http')
    async def project_boundary(request, call_next):
        if request.url.path.startswith('/api/') and request.headers.get('x-project-id') != os.environ.get('EVAL_PROJECT_ID'):
            return JSONResponse(status_code=403, content={'detail': 'Collector project mismatch'})
        if os.getenv('EVAL_COLLECTOR_LOCAL') == 'true' and request.url.path != '/health':
            exp = os.environ['EXPERIMENT_ID']
            allowed = {'/v1/traces', '/api/v1/evaluations/traces', '/api/v1/evaluations/experiments/' + exp, '/api/v1/evaluations/experiments/' + exp + '/samples'}
            if request.method != 'POST' or request.url.path not in allowed:
                return JSONResponse(status_code=403, content={'detail': 'Collector operation denied'})
        return await call_next(request)

    @app.exception_handler(StorageError)
    async def storage_error(request, error):
        return JSONResponse(status_code=404 if isinstance(error, NotFound) else 409 if isinstance(error, Conflict) else 503,
                            content={'detail': str(error)})

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/v1/traces', dependencies=[Depends(authorize)])
    def traces(value: dict, service=Depends(get_service)):
        project = os.environ['EVAL_PROJECT_ID']
        evaluation = os.environ['EXPERIMENT_ID']
        resources = value.get('resourceSpans')
        if not isinstance(resources, list):
            raise HTTPException(422, 'OTLP JSON resourceSpans is required')
        for resource in resources:
            for scope in resource.get('scopeSpans', []):
                for span in scope.get('spans', []):
                    trace_id, span_id = span.get('traceId'), span.get('spanId')
                    if not trace_id or not span_id:
                        raise HTTPException(422, 'traceId and spanId are required')
                    ident = f'{evaluation}:{trace_id}:{span_id}'
                    service.storage.put('traces', project, ident, {'id': ident,
                        'evaluation_id': evaluation, **trace_target(service, project, evaluation), 'created_at': now(), 'name': 'Execution trace' if policy.restricted else span.get('name', 'span'),
                        'data': policy.trace_data({'span': span, 'resource': resource.get('resource', {}), 'scope': scope.get('scope', {})})})
        return {}

    return app


def main():
    import uvicorn
    # Exporter choice is process-local; storage credentials stay in collector env.
    os.environ['EVAL_STORAGE_BACKEND'] = os.getenv('EVAL_COLLECTOR_EXPORTER', 'elasticsearch')
    uvicorn.run(create_collector(), host='127.0.0.1', port=int(os.getenv('EVAL_COLLECTOR_PORT', '18091')))


if __name__ == '__main__':
    main()

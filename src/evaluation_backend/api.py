"""HTTP composition root. Business and execution APIs will be added incrementally."""

from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
import os
import logging
from evaluation_backend.observability import configure_logging, event, instrument_app, shutdown_logging


def create_app() -> FastAPI:
    from evaluation_backend.config import load_config
    from evaluation_backend.models import router
    load_config()
    mode = os.getenv('EVAL_AUTH_MODE', 'token')
    if mode not in ('token', 'none'):
        raise ValueError('EVAL_AUTH_MODE must be token or none')
    @asynccontextmanager
    async def lifespan(app):
        configure_logging('evaluation-api')
        event('service.started')
        async def cleanup():
            from evaluation_backend.upload_api import reconcile_uploads
            while True:
                try: await asyncio.to_thread(reconcile_uploads)
                except Exception as error: event('upload.reconcile.failed', level='ERROR', error=error)
                await asyncio.sleep(15)
        task = asyncio.create_task(cleanup()) if os.getenv('KUBERNETES_SERVICE_HOST') or os.getenv('EVAL_EXECUTION_BACKEND') == 'docker' else None
        try: yield
        finally:
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            event('service.stopped')
            shutdown_logging()
    app = FastAPI(lifespan=lifespan, title="Evaluation Backend", version="0.1.0")
    from evaluation_backend.standalone import EvaluationBoundary
    app.add_middleware(EvaluationBoundary)
    origins = [v.strip() for v in os.getenv('EVAL_UI_ORIGINS', '').split(',') if v.strip()]
    if origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=['GET', 'POST', 'PATCH'],
                           allow_headers=['Content-Type', 'Authorization', 'X-Project-ID', 'X-Upload-Filename',
                                          'Idempotency-Key', 'X-Request-ID', 'traceparent'])
    instrument_app(app)

    app.include_router(router)
    from evaluation_backend.runs import router as runs_router
    app.include_router(runs_router)
    from evaluation_backend.storage.api import router as storage_router
    from evaluation_backend.storage import StorageError, NotFound, Conflict
    from fastapi.responses import JSONResponse
    app.include_router(storage_router)
    from evaluation_backend.upload_api import router as upload_router
    app.include_router(upload_router)
    from evaluation_backend.environments import router as environments_router
    app.include_router(environments_router)
    from evaluation_backend.target_configs import router as target_configs_router
    app.include_router(target_configs_router)

    @app.exception_handler(StorageError)
    async def storage_error(request, error):
        event('storage.request.failed', level='WARNING' if isinstance(error, (NotFound, Conflict)) else 'ERROR', error=error)
        status = 404 if isinstance(error, NotFound) else 409 if isinstance(error, Conflict) else 503
        return JSONResponse(status_code=status, content={'detail': str(error)})

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        # Process liveness only; does not imply OpenShell or a database is ready.
        return {"status": "ok", "service": "evaluation-backend"}

    return app


app = create_app()

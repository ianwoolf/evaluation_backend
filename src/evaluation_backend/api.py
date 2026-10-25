"""HTTP composition root. Business and execution APIs will be added incrementally."""

from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
import os
import logging


def create_app() -> FastAPI:
    from evaluation_backend.config import load_config
    from evaluation_backend.models import router
    load_config()
    @asynccontextmanager
    async def lifespan(app):
        async def cleanup():
            from evaluation_backend.upload_api import reconcile_uploads
            while True:
                try: await asyncio.to_thread(reconcile_uploads)
                except Exception: logging.getLogger(__name__).warning('Temporary upload cleanup failed; retrying')
                await asyncio.sleep(15)
        task = asyncio.create_task(cleanup()) if os.getenv('KUBERNETES_SERVICE_HOST') else None
        try: yield
        finally:
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
    app = FastAPI(lifespan=lifespan, title="Evaluation Backend", version="0.1.0")

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
        status = 404 if isinstance(error, NotFound) else 409 if isinstance(error, Conflict) else 503
        return JSONResponse(status_code=status, content={'detail': str(error)})

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        # Process liveness only; does not imply OpenShell or a database is ready.
        return {"status": "ok", "service": "evaluation-backend"}

    return app


app = create_app()

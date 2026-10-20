"""HTTP composition root. Business and execution APIs will be added incrementally."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    from evaluation_backend.config import load_config
    from evaluation_backend.models import router
    load_config()
    app = FastAPI(title="Evaluation Backend", version="0.1.0")

    app.include_router(router)
    from evaluation_backend.runs import router as runs_router
    app.include_router(runs_router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        # Process liveness only; does not imply OpenShell or a database is ready.
        return {"status": "ok", "service": "evaluation-backend"}

    return app


app = create_app()

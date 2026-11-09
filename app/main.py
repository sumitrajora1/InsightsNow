from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.auth import Authenticator, Policy
from app.config import Settings
from app.contracts import ChartChange, Identity, NewSession, QueryResponse, Question
from app.conversation import QueryService
from app.database import Base, Store
from app.errors import AppError
from app.execution import SQLiteDemoExecutor, TrinoExecutor
from app.llm import BedrockPlanner, MockPlanner
from app.metadata import FixtureMetadata, OpenMetadataAPI
from app.visualization import choose_chart


def create_app(settings: Settings | None = None, *, metadata=None, planner=None, executor=None):
    cfg = settings or Settings()
    Path(".runtime").mkdir(exist_ok=True)
    store = Store(cfg.database_url)
    auth, policy = Authenticator(cfg), Policy(cfg.policy_path)
    if cfg.authorization_provider == "trino_ranger":
        from app.enterprise import RangerPolicy

        policy = RangerPolicy(cfg, policy)
    service = QueryService(
        cfg,
        store,
        policy,
        metadata or (FixtureMetadata(cfg) if cfg.metadata_provider == "fixture" else OpenMetadataAPI(cfg)),
        planner or (MockPlanner() if cfg.llm_provider == "mock" else BedrockPlanner(cfg)),
        executor or (SQLiteDemoExecutor(cfg) if cfg.executor == "sqlite_demo" else TrinoExecutor(cfg)),
    )

    @asynccontextmanager
    async def lifespan(app):
        if cfg.environment != "production":
            Base.metadata.create_all(store.engine)
        # Production requires Alembic before startup; no automatic destructive migration.
        store.recover_interrupted()
        yield
        service.close()
        store.engine.dispose()

    app = FastAPI(title="English Data Query", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.store, app.state.settings = service, store, cfg

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.middleware("http")
    async def no_cache(request, call_next):
        if int(request.headers.get("content-length", "0") or 0) > 20000:
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "body_too_large", "message": "Request is too large."}},
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def user(request: Request):
        return auth.authenticate(request)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        try:
            with store.engine.connect() as c:
                c.execute(text("SELECT 1"))
            ok = service.metadata.health() and service.executor.health()
        except Exception:
            ok = False
        return JSONResponse(
            status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready"}
        )

    @app.get("/metrics")
    def prometheus(identity: Identity = Depends(user)):
        if "operations" not in identity.roles and cfg.environment == "production":
            raise AppError("access_denied", "Operations role required.", 403)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/me")
    def me(identity: Identity = Depends(user)):
        return {
            "username": identity.username,
            "development": cfg.auth_mode == "development",
            "synthetic": cfg.metadata_provider == "fixture",
            "executor": cfg.executor,
        }

    @app.post("/v1/sessions", status_code=201)
    def new_session(body: NewSession, identity: Identity = Depends(user)):
        policy.grant(identity)
        return {"id": store.create_session(identity.owner, body.title)}

    @app.get("/v1/sessions")
    def sessions(identity: Identity = Depends(user)):
        policy.grant(identity)
        return store.list_sessions(identity.owner)

    @app.get("/v1/sessions/{session_id}")
    def session(session_id: str, identity: Identity = Depends(user)):
        policy.grant(identity)
        row = store.conversation(identity.owner, session_id)
        history = [
            service.get(identity, r.id).model_dump(mode="json")
            for r in store.history(identity.owner, session_id, cfg.max_session_history)
        ]
        return {"id": row.id, "title": row.title, "history": history}

    @app.post("/v1/sessions/{session_id}/questions", response_model=QueryResponse, status_code=202)
    def question(session_id: str, body: Question, identity: Identity = Depends(user)):
        return service.submit(identity, session_id, body.question)

    @app.get("/v1/requests/{request_id}", response_model=QueryResponse)
    def request_status(request_id: str, identity: Identity = Depends(user)):
        return service.get(identity, request_id)

    @app.post("/v1/requests/{request_id}/cancel")
    def cancel(request_id: str, identity: Identity = Depends(user)):
        return service.cancel(identity, request_id)

    @app.post("/v1/requests/{request_id}/chart")
    def chart(request_id: str, body: ChartChange, identity: Identity = Depends(user)):
        row = service.get(identity, request_id)
        if not row.result:
            raise AppError("no_result", "There is no completed result to visualize.", 409)
        return choose_chart(row.result, body.chart_type)

    @app.get("/v1/requests/{request_id}/audit")
    def audit(request_id: str, identity: Identity = Depends(user)):
        service.get(identity, request_id)
        return store.audit_events(identity.owner, request_id)

    return app

"""FastAPI application entry point.

Run beside Streamlit without touching it::

    uvicorn portal.main:app --host 127.0.0.1 --port 8000

The portal reads the existing grading engine and rubric files but never writes
outside ``portal/``. No route here imports or calls the Classroom API.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from portal import __version__
from portal.config import PortalSettings, load_settings
from portal.db import build_engine, build_session_factory, init_db
from portal.exams import auto_check as exam_auto_check
from portal.grading.envguard import sanitize_no_proxy_env
from portal.grading.worker import GradingWorker
from portal.grading.worker import reset_stale as reset_grading_jobs
from portal.ingestion import catalog
from portal.ingestion.storage import ContentStore
from portal.ingestion.worker import ImportWorker
from portal.paths import CONTENT_DIR, STATIC_DIR, TEMPLATES_DIR
from portal.sandbox.executor import SandboxExecutor
from portal.sandbox.ratelimit import RunLimiter
from portal.sandbox.session import SandboxSessionStore
from portal.timeutil import format_local
from portal.web import (
    auth_routes,
    gradebook_routes,
    preview_routes,
    student_routes,
    sync_routes,
    teacher_routes,
)
from portal.web.deps import get_db, get_optional_user, render, role_home
from portal.models import User
from grading_engine.settings import settings as engine_settings

logger = logging.getLogger(__name__)

_ERROR_TITLES = {
    400: "בקשה לא תקינה",
    403: "אין הרשאה",
    404: "הדף לא נמצא",
}


def create_app(portal_settings: PortalSettings | None = None) -> FastAPI:
    cfg = portal_settings or load_settings()

    # The grading engine builds its HTTP client lazily, but a malformed
    # inherited NO_PROXY (a bare IPv6 entry) makes httpx reject that client at
    # construction and every grade fails. Normalise it before anything can
    # construct a client; the bypass rules themselves stay identical.
    if sanitize_no_proxy_env():
        logger.info("Normalised an IPv6 entry in NO_PROXY for the HTTP client.")

    engine = build_engine(cfg.database_url)
    session_factory = build_session_factory(engine)
    # create_all is idempotent; running it here (not only in lifespan) keeps
    # scripts and TestClient usage working without a server context.
    init_db(engine)

    content_store = ContentStore(CONTENT_DIR)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Catalog scans cannot resume after a restart; clear stale "running"
        # markers so the picker is not stuck on "סורק…" and can scan again.
        with session_factory() as db:
            catalog.reset_stale(db)
        # A grade is idempotent (the student's text is already saved), so an
        # interrupted grading job is simply queued again. Unlike a catalog
        # scan, it is worth resuming without asking the teacher.
        reset_grading_jobs(session_factory)
        worker = ImportWorker(session_factory, CONTENT_DIR)
        app.state.import_worker = worker
        worker.start()
        grading_worker = GradingWorker(session_factory)
        app.state.grading_worker = grading_worker
        grading_worker.start()
        # Automatic exam model pass: a quiet re-mark every 3 hours of the hand-ins whose
        # model verdict is still pending. Optional, and only meaningful while the
        # model pass itself is on (the module no-ops otherwise).
        auto_check = None
        if cfg.exam_auto_check_enabled:
            auto_check = exam_auto_check.ExamAutoCheckScheduler(
                session_factory,
                interval_seconds=cfg.exam_auto_check_interval_seconds,
                batch_size=cfg.exam_auto_check_batch_size,
            )
            app.state.exam_auto_check = auto_check
            auto_check.start()
        try:
            yield
        finally:
            if auto_check is not None:
                auto_check.stop()
            grading_worker.stop()
            worker.stop()
            sessions = getattr(app.state, "sandbox_sessions", None)
            if sessions is not None:
                sessions.close_all()
            engine.dispose()

    app = FastAPI(
        title=cfg.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.portal_settings = cfg
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.content_store = content_store
    app.state.sandbox_executor = SandboxExecutor()
    app.state.sandbox_limiter = RunLimiter()
    app.state.sandbox_sessions = SandboxSessionStore()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Cache-bust the shell assets so a browser that still holds an older
    # app.js/style.css picks up the current slide navigation on the next load.
    templates.env.globals["asset_version"] = max(
        int((STATIC_DIR / name).stat().st_mtime)
        for name in ("app.js", "style.css")
    )
    templates.env.filters["local_dt"] = lambda value: format_local(
        value, cfg.timezone
    )
    templates.env.filters["local_date"] = lambda value: format_local(
        value, cfg.timezone, "%d/%m/%Y"
    )
    # Value for <input type="datetime-local">: wall-clock time in the school's
    # zone, exactly the format the browser control round-trips.
    templates.env.filters["local_input"] = lambda value: format_local(
        value, cfg.timezone, "%Y-%m-%dT%H:%M"
    )
    # Value for <input type="date">: the calendar day in the school's zone.
    templates.env.filters["local_date_input"] = lambda value: format_local(
        value, cfg.timezone, "%Y-%m-%d"
    )
    templates.env.filters["basename"] = lambda value: Path(str(value)).name
    # One number shown to teachers and students, read from the same setting the
    # adapter hands to src.late_policy - never a second, drifting value.
    templates.env.globals["late_penalty_points"] = engine_settings.late_penalty_points
    app.state.templates = templates

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.secret_key,
        session_cookie=cfg.session_cookie,
        max_age=cfg.session_max_age,
        same_site="lax",
        https_only=cfg.https_only,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    static_dir = Path(STATIC_DIR)
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(auth_routes.router)
    app.include_router(teacher_routes.router)
    app.include_router(preview_routes.router)
    app.include_router(gradebook_routes.router)
    app.include_router(sync_routes.router)
    app.include_router(student_routes.router)

    @app.get("/healthz")
    def healthz(db: Session = Depends(get_db)):
        db.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "service": "portal",
            "version": __version__,
            "environment": cfg.environment,
        }

    @app.get("/")
    def index(user: User | None = Depends(get_optional_user)):
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse(role_home(user), status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        location = (exc.headers or {}).get("Location")
        if location and 300 <= exc.status_code < 400:
            return RedirectResponse(location, status_code=exc.status_code)
        if exc.status_code in _ERROR_TITLES:
            return render(
                request,
                "error.html",
                status_code=exc.status_code,
                title=_ERROR_TITLES[exc.status_code],
                message=str(exc.detail),
            )
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    return app


_cached_app: FastAPI | None = None


def __getattr__(name: str):
    """Create ``app`` lazily for ``uvicorn portal.main:app``.

    Importing this module (for example from the test-suite, which builds its
    own app with a temporary database) must not create the default database or
    signing key as a side effect.
    """
    global _cached_app
    if name == "app":
        if _cached_app is None:
            _cached_app = create_app()
        return _cached_app
    raise AttributeError(name)

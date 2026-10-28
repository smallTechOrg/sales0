"""FastAPI application factory: routers, static files, middleware, startup hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sales0 import __version__
from sales0.config import Settings, get_settings
from sales0.db.engine import get_engine, session_scope
from sales0.db.migrate_check import assert_at_head
from sales0.jobs.reply_poller import get_poller
from sales0.logging import configure_logging, get_logger
from sales0.services.keywords import seed_suggested_once
from sales0.services.runs import recover_interrupted_runs_on_startup
from sales0.services.team.members import seed_admin
from sales0.web.envelope import install_exception_handlers
from sales0.web.middleware import CsrfOriginMiddleware, RequestContextMiddleware
from sales0.web.routes import (
    costs,
    digest,
    health,
    inbox,
    oauth,
    outreach,
    replies,
    runs,
    schedule,
    setup,
    stubs,
    suggestions,
    tracked,
)
from sales0.web.templating import STATIC_DIR

log = get_logger("sales0.app")

FIXTURE_PREFIX = "/__fixtures/linkedin"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        revision = assert_at_head(get_engine())
        recover_interrupted_runs_on_startup()  # Phase 1.4: queued/running runs -> aborted (restart)
        with session_scope() as db:
            seed_suggested_once(db)  # Phase 1.4: the 60 suggested keywords, once
            seed_admin(db, settings)  # Phase 3: first admin from SALES0_ADMIN_EMAIL (idempotent)
        log.info(
            "app_started",
            db_revision=revision,
            linkedin_mode=settings.linkedin_mode,
            model=settings.llm_model,
            api_key_present=settings.api_key_present,
        )
        from sales0.jobs.scheduler import get_scheduler

        scheduler = get_scheduler()
        if settings.scheduler_enabled:
            scheduler.start()  # persisted scheduled_jobs state; jobs are opt-in on /schedule
        poller = get_poller()
        poller.start()  # no-op when SALES0_REPLY_POLL_MINUTES=0
        yield
        poller.stop()
        scheduler.stop()
        log.info("app_stopped")

    app = FastAPI(title="sales0", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CsrfOriginMiddleware)  # inner: runs after request-id binding
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(stubs.router)
    app.include_router(setup.router)
    app.include_router(runs.router)
    app.include_router(inbox.router)
    app.include_router(oauth.router)
    app.include_router(outreach.router)
    app.include_router(costs.router)
    app.include_router(schedule.router)
    app.include_router(digest.router)
    app.include_router(suggestions.router)
    app.include_router(replies.router)
    app.include_router(tracked.router)

    if settings.linkedin_mode == "fixture":
        from sales0.linkedin_fixture.routes import router as fixture_router

        app.include_router(fixture_router, prefix=FIXTURE_PREFIX)

    return app

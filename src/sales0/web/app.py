"""FastAPI application factory: routers, static files, middleware, startup hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sales0 import __version__
from sales0.config import Settings, get_settings
from sales0.datalock import DataDirLock
from sales0.db.engine import get_engine, harden_storage, session_scope
from sales0.db.migrate_check import assert_at_head
from sales0.jobs.reply_poller import get_poller
from sales0.logging import configure_logging, get_logger
from sales0.services import crm
from sales0.services.crm import digest_sections as crm_digest_sections
from sales0.services.keyword_library_seed import seed_new_phrases
from sales0.services.keywords import seed_suggested_once
from sales0.services.metrics import digest_section as metrics_digest_section
from sales0.services.runs import recover_interrupted_runs_on_startup
from sales0.services.sheetsync import sql_repository
from sales0.services.team import accounts
from sales0.services.team.members import seed_admin
from sales0.web import auth as web_auth
from sales0.web.envelope import install_exception_handlers
from sales0.web.middleware import CsrfOriginMiddleware, HardeningMiddleware, RequestContextMiddleware
from sales0.web.routes import (
    company,
    contacts,
    costs,
    criteria,
    digest,
    health,
    importer,
    inbox,
    metrics,
    oauth,
    outreach,
    pipeline,
    registries,
    replies,
    runs,
    schedule,
    setup,
    stubs,
    suggestions,
    sync,
    tracked,
    users,
    worker_api,
)
from sales0.web.templating import STATIC_DIR

log = get_logger("sales0.app")

FIXTURE_PREFIX = "/__fixtures/linkedin"


# Launch options shared by __main__ and the tests' live servers. proxy_headers=False: uvicorn would otherwise
# rewrite request.client from a spoofable X-Forwarded-For, defeating the per-IP login throttle.
UVICORN_OPTIONS: dict[str, bool] = {"proxy_headers": False}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def _lifespan_body(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        revision = assert_at_head(get_engine())
        harden_storage(settings)
        recover_interrupted_runs_on_startup()  # Phase 1.4: queued/running runs -> aborted (restart)
        with session_scope() as db:
            seed_suggested_once(db)  # Phase 1.4: the 60 suggested keywords, once
            seed_new_phrases(db)  # fixscrape: v2 phrases for DBs that took v1
            seed_admin(db, settings)  # Phase 3: first admin from SALES0_ADMIN_EMAIL (idempotent)
            accounts.bootstrap_admin(db, settings)  # Phase 5: SALES0_APP_PASSWORD = first admin's initial password
        crm.install()  # Phase 2: CRM event hooks, SQL sheet repository, CRM digest sections
        sql_repository.install()
        crm_digest_sections.register()
        metrics_digest_section.register()  # Phase 4: 'Weekly pulse' digest section
        with session_scope() as db:
            crm.startup_backfill(db)  # once per DB (app_meta crm_flags_backfilled_v1)
        log.info(
            "app_started",
            mode=settings.mode,
            db_revision=revision,
            linkedin_mode=settings.linkedin_mode,
            model=settings.llm_model,
            api_key_present=settings.api_key_present,
        )
        from sales0.jobs.scheduler import get_scheduler

        scheduler = get_scheduler()
        if settings.scheduler_enabled and not settings.is_hosted:  # hosted: every job is a manual action
            scheduler.start()  # persisted scheduled_jobs state; jobs are opt-in on /schedule
        poller = get_poller()
        poller.start()  # no-op when SALES0_REPLY_POLL_MINUTES=0
        yield
        poller.stop()
        scheduler.stop()
        log.info("app_stopped")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Fixscrape D (#5): one sales0 process per data dir, BEFORE recover_interrupted_runs_on_startup.
        settings.data_path.mkdir(parents=True, exist_ok=True)
        lock = DataDirLock(settings.data_path)
        # Hosted: PostgreSQL is the shared state and no run lives in this process, so there is nothing a second
        # instance could corrupt (and /tmp is per instance anyway): no data-dir lock.
        if not settings.allow_multi_process and not settings.is_hosted:
            lock.acquire()  # raises DataDirLocked with the explanation when another process holds it
        try:
            async with _lifespan_body(_app):
                yield
        finally:
            lock.release()

    app = FastAPI(title="sales0", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CsrfOriginMiddleware)  # innermost
    web_auth.install(app, settings)  # Phase 2 (S3): AuthMiddleware + login router; between CSRF and request-context
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(HardeningMiddleware)  # outermost: body-size cap + security headers on every response
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
    app.include_router(metrics.router)
    app.include_router(users.router)  # Phase 5: admin Users page
    app.include_router(criteria.router)  # inbox feedback A3: editable triage criteria + discard reasons
    app.include_router(worker_api.router)  # hosted: the laptop worker (bearer token; 404 without SALES0_WORKER_TOKEN)
    for r in (pipeline.router, company.router, contacts.router, registries.router, importer.router, sync.router, tracked.router):
        app.include_router(r)
    from sales0.web.routes import prospect_research

    app.include_router(prospect_research.router)  # q15: prospect research, account summary, our notes

    if settings.linkedin_mode == "fixture":
        from sales0.linkedin_fixture.routes import router as fixture_router

        app.include_router(fixture_router, prefix=FIXTURE_PREFIX)

    return app

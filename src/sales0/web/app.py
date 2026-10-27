"""FastAPI application factory: routers, static files, middleware, startup hooks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sales0 import __version__
from sales0.config import Settings, get_settings
from sales0.db.engine import get_engine
from sales0.db.migrate_check import assert_at_head
from sales0.logging import configure_logging, get_logger
from sales0.web.envelope import install_exception_handlers
from sales0.web.middleware import RequestContextMiddleware
from sales0.web.routes import health, inbox, runs, setup, stubs
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
        log.info(
            "app_started",
            db_revision=revision,
            linkedin_mode=settings.linkedin_mode,
            model=settings.llm_model,
            api_key_present=settings.api_key_present,
        )
        yield
        log.info("app_stopped")

    app = FastAPI(title="sales0", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestContextMiddleware)
    install_exception_handlers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(stubs.router)
    app.include_router(setup.router)
    app.include_router(runs.router)
    app.include_router(inbox.router)

    if settings.linkedin_mode == "fixture":
        from sales0.linkedin_fixture.routes import router as fixture_router

        app.include_router(fixture_router, prefix=FIXTURE_PREFIX)

    return app

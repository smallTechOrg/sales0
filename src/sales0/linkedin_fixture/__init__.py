"""Fake LinkedIn for offline tests and demos (slice C).

Mounted at ``/__fixtures/linkedin`` ONLY when ``SALES0_LINKEDIN_MODE=fixture`` (see
``sales0.web.app``). Its DOM matches ``sales0.scraper.selectors`` exactly, so the same
scraper code runs against it and against live LinkedIn.
"""

from __future__ import annotations

from fastapi import FastAPI

from sales0.scraper.urls import FIXTURE_PREFIX


def build_app() -> FastAPI:
    """A standalone app serving only the fixture routes at ``FIXTURE_PREFIX`` (for test servers)."""
    from sales0.linkedin_fixture.routes import router

    app = FastAPI(title="sales0 fixture LinkedIn", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router, prefix=FIXTURE_PREFIX)
    return app

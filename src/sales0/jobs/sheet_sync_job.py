"""Wires the EXISTING ``SheetSyncRunner`` (services/sheetsync) into the scheduler.

Nothing here re-implements syncing: ``build_runner`` constructs the runner with the same
engine/fetcher/tabs the ``sync-sheets`` CLI uses and a ``SqlStateStore`` so its interval
survives restarts. The scheduler just calls ``runner.tick()`` while the job is enabled.
"""

from __future__ import annotations

from datetime import datetime

from sales0.config import Settings
from sales0.db.engine import get_sessionmaker
from sales0.services.sheetsync import (
    SheetFetcher,
    SheetSyncRunner,
    SqlStateStore,
    SyncEngine,
    SyncReport,
    get_repository,
    make_client,
    tabs_from_settings,
)

NO_REPOSITORY = "Waiting for the Phase 2 CRM tables - there is nowhere to sync into yet."


def repository_available() -> bool:
    return get_repository() is not None


def run_sync(settings: Settings) -> SyncReport:
    repo = get_repository()
    if repo is None:
        raise RuntimeError(NO_REPOSITORY)
    fetcher = SheetFetcher(
        make_client(timeout_s=settings.sheet_fetch_timeout_s),
        retries=settings.sheet_fetch_retries,
        max_bytes=settings.sheet_fetch_max_bytes,
    )
    try:
        engine = SyncEngine(fetcher, repo, tabs_from_settings(settings), today=lambda: datetime.now(settings.tz).date())  # type: ignore[arg-type]
        return engine.run(dry_run=False, only_tab=None)
    finally:
        fetcher.close()


def build_runner(settings: Settings, **overrides) -> SheetSyncRunner:  # type: ignore[no-untyped-def]
    kwargs = {
        "job": lambda: run_sync(settings),
        "interval_min": settings.sheet_sync_minutes,
        "state": SqlStateStore(get_sessionmaker()),
    }
    kwargs.update(overrides)
    return SheetSyncRunner(**kwargs)

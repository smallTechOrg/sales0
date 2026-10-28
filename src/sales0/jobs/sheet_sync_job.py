"""Wires the EXISTING ``SheetSyncRunner`` (services/sheetsync) into the scheduler.

Nothing here re-implements syncing: ``build_runner`` constructs the runner with the same
engine/fetcher/tabs the ``sync-sheets`` CLI uses and a ``SqlStateStore`` so its interval
survives restarts. The scheduler just calls ``runner.tick()`` while the job is enabled.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sales0.config import Settings
from sales0.db.engine import get_sessionmaker
from sales0.services.sheetsync.fetch import TabSource
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

NO_REPOSITORY = "The Phase 2 CRM sheet repository is not installed (the app installs it at startup), so there is nowhere to sync into."

# Test seam: a callable returning a ``TabSource`` (e.g. ``StaticSource``) so the runner never
# touches the network. ``None`` = the real read-only fetcher.
_source_factory: Callable[[], TabSource] | None = None


def set_source_factory(factory: Callable[[], TabSource] | None) -> None:
    global _source_factory
    _source_factory = factory


def repository_available() -> bool:
    """Ready once ``sql_repository.install()`` has registered the CRM repository."""
    return get_repository() is not None


def run_sync(settings: Settings, *, allow_mass_removal: bool = False) -> SyncReport:
    repo = get_repository()
    if repo is None:
        raise RuntimeError(NO_REPOSITORY)
    fetcher = None
    if _source_factory is not None:
        source: TabSource = _source_factory()
    else:
        fetcher = SheetFetcher(
            make_client(timeout_s=settings.sheet_fetch_timeout_s),
            retries=settings.sheet_fetch_retries,
            max_bytes=settings.sheet_fetch_max_bytes,
        )
        source = fetcher
    try:
        engine = SyncEngine(
            source, repo, tabs_from_settings(settings), today=lambda: datetime.now(settings.tz).date(), allow_mass_removal=allow_mass_removal
        )
        return engine.run(dry_run=False, only_tab=None)
    finally:
        if fetcher is not None:
            fetcher.close()


def build_runner(settings: Settings, **overrides) -> SheetSyncRunner:  # type: ignore[no-untyped-def]
    kwargs = {
        "job": lambda: run_sync(settings),
        "mass_removal_job": lambda: run_sync(settings, allow_mass_removal=True),
        "interval_min": settings.sheet_sync_minutes,
        "state": SqlStateStore(get_sessionmaker()),
    }
    kwargs.update(overrides)
    return SheetSyncRunner(**kwargs)

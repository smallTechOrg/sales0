"""Sheet sync into the CRM: a migrated temp SQLite DB, the real engine, the real SqlSheetRepository,
synthetic CSV text through ``StaticSource`` (no network)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sales0.services.sheetsync import StaticSource, SyncEngine, SyncReport, TabSpec
from sales0.services.sheetsync.sql_repository import SqlSheetRepository
from tests.unit.sheetsync import fixtures as fx

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sheets"
ACTIVE = fx.TABS[0]

Sync = Callable[..., SyncReport]


def matrix_csv() -> str:
    return (FIXTURES / "active_status_matrix.csv").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_registry():  # type: ignore[no-untyped-def]
    """The repository registry is process-global (the app lifespan installs into it): start and end clean."""
    from sales0.services.sheetsync import register_repository_factory

    register_repository_factory(None)
    yield
    register_repository_factory(None)


@pytest.fixture
def sync(db_url: str) -> Sync:
    """``sync(texts=None, dry_run=False, only_tab=None, tabs=None, clock=None, repo=None)``."""

    def run(
        texts: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
        only_tab: str | None = None,
        tabs: tuple[TabSpec, ...] | None = None,
        now: datetime = NOW,
        repo: SqlSheetRepository | None = None,
    ) -> SyncReport:
        repo = repo or SqlSheetRepository(clock=lambda: now)
        engine = SyncEngine(StaticSource(texts if texts is not None else fx.all_tabs_csv()), repo, tabs or fx.TABS, today=lambda: fx.TODAY, now=lambda: now)
        return engine.run(dry_run=dry_run, only_tab=only_tab)

    return run


def table_counts() -> dict[str, int]:
    from sqlalchemy import func, select

    from sales0.db.engine import session_scope
    from sales0.db.models import Enquiry
    from sales0.db.models.contacts import Contact
    from sales0.db.models.crm import Client, Company, Lead, Prospect
    from sales0.db.models.registries import ChannelGroup, Competitor
    from sales0.db.models.sheetdata import SheetMetric, SheetMetricWeek, SheetSource
    from sales0.db.models.timeline import TimelineEvent

    models = (Enquiry, Prospect, Lead, Client, Company, Contact, Competitor, ChannelGroup, SheetMetric, SheetMetricWeek, SheetSource, TimelineEvent)
    with session_scope() as db:
        return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in models}

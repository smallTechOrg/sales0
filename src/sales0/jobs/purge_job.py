"""Daily retention purge of ``seen_posts`` raw text (AUDIT_SCRAPER #6, AUDIT_DATA #11/#12).

* NULL ``raw_purge_after`` on a row that still holds text is backfilled in code (no migration):
  ``first_seen_at + seen_raw_retention_days``; idempotent (only NULL rows are touched).
* Then ``seen_posts.purge_expired`` drops the text of triaged (``done``) rows past their date.
  Pending/failed rows keep their text so triage can still be retried.
* Logs counts only (no post text, no names).

Scheduler wiring (done in ``jobs/scheduler.py``, next to the other hooks)::

    from sales0.jobs import purge_job
    register_purge_hook(purge_job.HOOK_NAME, purge_job.purge_hook)

This module deliberately does not import the scheduler (no import cycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import get_settings
from sales0.db.models import SeenPost
from sales0.logging import get_logger
from sales0.services import seen_posts

log = get_logger("sales0.jobs.purge")

HOOK_NAME = "seen_posts"


@dataclass(frozen=True)
class PurgeResult:
    backfilled: int
    purged: int


def backfill_purge_dates(db: Session, *, retention_days: int, now: datetime | None = None) -> int:  # noqa: ARG001
    rows = db.scalars(
        select(SeenPost).where(SeenPost.raw_text.is_not(None), SeenPost.raw_purge_after.is_(None))
    ).all()
    for row in rows:
        row.raw_purge_after = row.first_seen_at + timedelta(days=retention_days)
    db.flush()
    return len(rows)


def run_purge(db: Session, *, now: datetime | None = None, retention_days: int | None = None) -> PurgeResult:
    """Backfill NULL purge dates, then purge expired text of triaged rows. Flushes, never commits."""
    stamp = now or utcnow()
    days = retention_days if retention_days is not None else get_settings().seen_raw_retention_days
    backfilled = backfill_purge_dates(db, retention_days=days, now=stamp)
    purged = seen_posts.purge_expired(db, now=stamp)
    log.info("seen_posts_purge_run", backfilled=backfilled, purged=purged)
    return PurgeResult(backfilled=backfilled, purged=purged)


def purge_hook(db: Session, cutoff: datetime) -> int:  # noqa: ARG001
    """Scheduler ``PurgeHook`` signature: ``(db, cutoff) -> rows_removed`` (the cutoff is ignored:
    seen_posts carry their own ``raw_purge_after``)."""
    return run_purge(db).purged


__all__ = ["HOOK_NAME", "PurgeResult", "backfill_purge_dates", "purge_hook", "run_purge"]

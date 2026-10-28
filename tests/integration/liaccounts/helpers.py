"""Seeders for the LinkedIn-accounts tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun
from sales0.services import linkedin_accounts

T0 = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)


def new_account(label: str = "Second", *, backend: str = "folder", status: str = "connected") -> int:
    with session_scope() as db:
        view = linkedin_accounts.add_account(db, label, backend=backend)
        linkedin_accounts.get_account(db, view.id).status = status
        return view.id


def set_default_status(status: str = "connected") -> int:
    with session_scope() as db:
        row = linkedin_accounts.resolve_account(db, None)
        row.status = status
        return row.id


def add_run(account_id: int | None, *, status: str = "succeeded", abort: str | None = None,
            posts_seen: int = 0, at: datetime = T0) -> int:
    with session_scope() as db:
        run = ScrapeRun(
            trigger="manual", mode="live", status=status, abort_reason=abort,
            started_at=at - timedelta(minutes=1), finished_at=at, posts_seen=posts_seen,
            linkedin_account_id=account_id,
        )
        db.add(run)
        db.flush()
        return run.id

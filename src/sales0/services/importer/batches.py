"""``import_batches`` persistence and views."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import ImportBatch
from sales0.services._common import not_found
from sales0.services.crm.actor import Actor
from sales0.services.importer.views import ImportBatchView, ImportIssueView

MAX_STORED_ISSUES = 500


def _issues_json(issues: list[ImportIssueView]) -> list[dict[str, Any]]:
    return [{"row": i.row, "code": i.code, "level": i.level} for i in issues[:MAX_STORED_ISSUES]]


def view_of(row: ImportBatch) -> ImportBatchView:
    issues = [
        ImportIssueView(int(e.get("row", 0)), str(e.get("code", "")), e.get("level", "error"))
        for e in (row.errors or [])
    ]
    ignored = [i.code.split(":", 1)[1] for i in issues if i.code.startswith("ignored-column:")]
    return ImportBatchView(
        id=row.id, filename=row.filename, kind=row.kind, rows_total=row.rows_total, rows_new=row.rows_new,
        rows_merged=row.rows_merged, rows_unchanged=row.rows_unchanged, rows_error=row.rows_error,
        errors=[i for i in issues if not i.code.startswith("ignored-column:")], ignored_columns=ignored,
        dry_run=False, actor=row.actor, created_at=row.created_at,
    )  # fmt: skip


def record_batch(
    db: Session, *, filename: str, kind: str, actor: Actor, counts: tuple[int, int, int, int, int],
    issues: list[ImportIssueView], now: datetime,
) -> ImportBatch:  # fmt: skip
    total, new, merged, unchanged, error = counts
    row = ImportBatch(
        filename=filename[:255], kind=kind, rows_total=total, rows_new=new, rows_merged=merged,
        rows_unchanged=unchanged, rows_error=error, errors=_issues_json(issues), actor=actor.label[:80],
        actor_member_id=actor.member_id, created_at=now,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def get_batch(db: Session, batch_id: int) -> ImportBatchView:
    row = db.get(ImportBatch, batch_id)
    if row is None:
        raise not_found()
    return view_of(row)


def list_batches(db: Session, limit: int = 20) -> list[ImportBatchView]:
    rows = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit)).all()
    return [view_of(r) for r in rows]

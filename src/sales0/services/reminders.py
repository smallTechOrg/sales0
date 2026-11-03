"""Reminders: a small, reusable "remind me on <date> to <note>" module for any record (``subject_type`` +
``subject_id``: a prospect today, any other feature tomorrow). The Due panel and the nav badge show the open ones
that are due; Done / Change date close or move them. The follow-up log creates them from free text ("call again
next Tuesday"), nothing is pre-scheduled.

Functions take ``db`` first and flush, never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Company, Prospect, Reminder
from sales0.errors import DomainError
from sales0.services._common import clip, not_found, now_or

_OPEN = (Reminder.done_at.is_(None), Reminder.cancelled_at.is_(None))


@dataclass(frozen=True)
class ReminderView:
    id: int
    due_on: date
    note: str
    overdue: bool
    subject_type: str
    subject_id: int
    company_id: int | None
    prospect_id: int | None
    who: str
    href: str
    source: str
    done: bool = False


def _today(now: datetime | None = None) -> date:
    return local_today(get_settings().tz, now)


def _href(r: Reminder) -> str:
    if r.company_id:
        return f"/companies/{r.company_id}"
    if r.prospect_id:
        return f"/pipeline/prospects/{r.prospect_id}"
    return "/pipeline"


def views(db: Session, rows: list[Reminder], today: date | None = None) -> list[ReminderView]:
    today = today or _today()
    cids = {r.company_id for r in rows if r.company_id}
    pids = {r.prospect_id for r in rows if r.prospect_id}
    companies = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(cids))).tuples().all()) if cids else {}
    prospects = dict(db.execute(select(Prospect.id, Prospect.name).where(Prospect.id.in_(pids))).tuples().all()) if pids else {}
    return [
        ReminderView(
            id=r.id, due_on=r.due_on, note=r.note, overdue=r.done_at is None and r.due_on < today, subject_type=r.subject_type,
            subject_id=r.subject_id, company_id=r.company_id, prospect_id=r.prospect_id,
            who=companies.get(r.company_id) or prospects.get(r.prospect_id) or f"{r.subject_type} {r.subject_id}", href=_href(r),
            source=r.source, done=r.done_at is not None,
        )
        for r in rows
    ]


def _check_date(d: object) -> date:
    if not isinstance(d, date):
        raise DomainError("reminder_invalid", "Pick a date for the reminder.", 400)
    if d.year < 2000 or d.year > 2100:
        raise DomainError("reminder_invalid", "That date is out of range.", 400)
    return d


def add(
    db: Session, *, subject_type: str, subject_id: int, due_on: date, note: str = "", company_id: int | None = None,
    prospect_id: int | None = None, source: str = "manual", created_by: str = "team", owner_member_id: int | None = None,
    now: datetime | None = None,
) -> Reminder:
    row = Reminder(
        subject_type=subject_type[:20], subject_id=int(subject_id), due_on=_check_date(due_on), note=clip(" ".join((note or "").split()), 300) or "",
        company_id=company_id, prospect_id=prospect_id, source=source[:20], created_by=(created_by or "team")[:80],
        owner_member_id=owner_member_id, created_at=now_or(now), updated_at=now_or(now),
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def _get(db: Session, reminder_id: int) -> Reminder:
    row = db.get(Reminder, reminder_id)
    if row is None:
        raise not_found()
    return row


def complete(db: Session, reminder_id: int, *, actor: str = "team", now: datetime | None = None) -> Reminder:
    row = _get(db, reminder_id)
    if row.done_at is None and row.cancelled_at is None:
        row.done_at, row.done_by = now_or(now), actor[:80]
        db.flush()
    return row


def snooze(db: Session, reminder_id: int, due_on: date, *, now: datetime | None = None) -> Reminder:
    row = _get(db, reminder_id)
    if row.done_at is not None or row.cancelled_at is not None:
        raise DomainError("reminder_closed", "That reminder is already done.", 409)
    row.due_on, row.updated_at = _check_date(due_on), now_or(now)
    db.flush()
    return row


def cancel(db: Session, reminder_id: int, *, now: datetime | None = None) -> Reminder:
    row = _get(db, reminder_id)
    if row.done_at is None and row.cancelled_at is None:
        row.cancelled_at = now_or(now)
        db.flush()
    return row


def open_for(db: Session, *, subject_type: str | None = None, subject_id: int | None = None, prospect_id: int | None = None,
             company_id: int | None = None, today: date | None = None) -> list[ReminderView]:
    stmt = select(Reminder).where(*_OPEN)
    if subject_type is not None:
        stmt = stmt.where(Reminder.subject_type == subject_type, Reminder.subject_id == subject_id)
    if prospect_id is not None:
        stmt = stmt.where(Reminder.prospect_id == prospect_id)
    if company_id is not None:
        stmt = stmt.where(Reminder.company_id == company_id)
    return views(db, list(db.scalars(stmt.order_by(Reminder.due_on, Reminder.id))), today)


def _due_stmt(today: date, member_id: int | None = None):  # type: ignore[no-untyped-def]
    stmt = select(Reminder).where(*_OPEN, Reminder.due_on <= today)
    if member_id is not None:
        stmt = stmt.where((Reminder.owner_member_id == member_id) | Reminder.owner_member_id.is_(None))
    return stmt


def due(db: Session, *, today: date | None = None, member_id: int | None = None) -> list[ReminderView]:
    """Open reminders due today or overdue (oldest first)."""
    today = today or _today()
    return views(db, list(db.scalars(_due_stmt(today, member_id).order_by(Reminder.due_on, Reminder.id))), today)


def due_count(db: Session, *, today: date | None = None) -> int:
    today = today or _today()
    return int(db.scalar(select(func.count()).select_from(_due_stmt(today).subquery())) or 0)


__all__ = ["ReminderView", "add", "cancel", "complete", "due", "due_count", "open_for", "snooze", "views"]

"""CRM digest sections: follow-ups due, stale leads, pipeline movement.

Registered through ``digest.register_section`` (idempotent: the same key replaces itself). The digest
builds every section in a savepoint, so a failing CRM query shows an error item and nothing else
changes. "Today" is the local calendar date of the digest window's end, so a rebuilt past digest and
a test with a fixed clock are both deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import Company, Enquiry, Lead, Prospect, TimelineEvent
from sales0.services import digest
from sales0.services.crm import followups
from sales0.services.crm.stages import STAGE_LABELS
from sales0.services.crm.views import FollowUpView
from sales0.services.digest import DigestSection, Item, SectionResult, Window

LIST_CAP = digest.LIST_CAP  # 10


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------------- follow-ups due
def _followup_item(db: Session, fu: FollowUpView, today: date) -> Item:
    company: str | None = None
    if fu.prospect_id is not None:
        company = db.scalar(
            select(Company.name).join(Prospect, Prospect.company_id == Company.id).where(Prospect.id == fu.prospect_id)
        )
        href = f"/pipeline/prospects/{fu.prospect_id}"
    else:
        company = db.scalar(
            select(Company.name).join(Lead, Lead.company_id == Company.id).where(Lead.id == fu.lead_id)
        )
        href = f"/pipeline/leads/{fu.lead_id}"
    head = company or fu.who
    when = "due today" if fu.due_on >= today else f"overdue {(today - fu.due_on).days}d"
    text = f"{head} - {fu.kind_label} {when}"
    if company and fu.who and fu.who.casefold() != company.casefold():
        text += f" ({fu.who})"
    return {"text": text, "href": href}


def _follow_ups_due(db: Session, w: Window) -> SectionResult:
    today = local_today(get_settings().tz, w.end)
    panel = followups.list_due(db, today=today)
    ordered = [*sorted(panel.overdue, key=lambda f: (f.due_on, f.id)), *panel.due_today]
    items = [_followup_item(db, fu, today) for fu in ordered[:LIST_CAP]]
    return SectionResult(
        items, {"due_today": len(panel.due_today), "overdue": len(panel.overdue)}, "No follow-ups due."
    )


# ---------------------------------------------------------------------- stale leads
def _stale_leads(db: Session, w: Window) -> SectionResult:
    days = get_settings().stale_lead_days
    cutoff = w.end - timedelta(days=days)
    last_event = (
        select(TimelineEvent.prospect_id.label("pid"), func.max(TimelineEvent.occurred_at).label("last_at"))
        .where(TimelineEvent.prospect_id.is_not(None), TimelineEvent.occurred_at <= w.end)
        .group_by(TimelineEvent.prospect_id)
        .subquery()
    )
    # A prospect fed only by enquiries that left the sheet is hidden, so it is not "stale" either.
    live_enquiry = (
        select(Enquiry.id).where(Enquiry.prospect_id == Prospect.id, Enquiry.sheet_removed_at.is_(None)).exists()
    )
    any_enquiry = select(Enquiry.id).where(Enquiry.prospect_id == Prospect.id).exists()
    gone_with_sheet = any_enquiry & ~live_enquiry
    rows = db.execute(
        select(Lead, Prospect.name, Company.name, last_event.c.last_at)
        .join(Prospect, Prospect.id == Lead.prospect_id)
        .outerjoin(Company, Company.id == Lead.company_id)
        .outerjoin(last_event, last_event.c.pid == Lead.prospect_id)
        .where(Lead.closed_at.is_(None), Lead.opened_at <= w.end, Prospect.sheet_removed_at.is_(None), ~gone_with_sheet)
        .order_by(Lead.id)
    ).all()
    stale: list[tuple[datetime, Lead, str]] = []
    for lead, prospect_name, company_name, last_at in rows:
        newest = max(t for t in (last_at, lead.opened_at) if t)  # no event at all: the lead's own opening
        if newest < cutoff:
            stale.append((newest, lead, company_name or prospect_name))
    stale.sort(key=lambda t: (t[0], t[1].id))
    items: list[Item] = []
    for newest, lead, who in stale[:LIST_CAP]:
        quiet = (w.end - newest).days
        stage = STAGE_LABELS.get(lead.stage, lead.stage)
        items.append({"text": f"{who} - {stage}, quiet for {_plural(quiet, 'day')}", "href": f"/pipeline/leads/{lead.id}"})
    return SectionResult(items, {"stale": len(stale), "open_leads": len(rows)}, "No stale leads.")


# ---------------------------------------------------------------------- pipeline movement
def _count(db: Session, model: Any, col: Any, w: Window, *where: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(digest._between(col, w), *where)) or 0)  # noqa: SLF001


MOVEMENT_LABELS = (  # stat key, singular, plural, link
    ("new_prospects", "new prospect", "new prospects", "/pipeline"),
    ("contacted", "contacted", "contacted", "/pipeline"),
    ("replies", "reply", "replies", "/replies"),
    ("leads_opened", "lead opened", "leads opened", "/pipeline?tab=leads"),
    ("won", "won", "won", "/pipeline?tab=leads"),
)


def _pipeline_movement(db: Session, w: Window) -> SectionResult:
    # Sheet-fed rows (first sync, later edits) are bookkeeping, not team activity: they are not counted.
    stats = {
        "new_prospects": _count(db, Prospect, Prospect.created_at, w, Prospect.source != "sheet"),
        "contacted": _count(db, Prospect, Prospect.contacted_at, w, Prospect.contacted_estimated.is_(False)),
        "replies": _count(db, TimelineEvent, TimelineEvent.occurred_at, w, TimelineEvent.kind == "reply_received"),
        "leads_opened": _count(db, Lead, Lead.opened_at, w, Lead.opened_by != "sheet"),
        "won": _count(db, Lead, Lead.closed_at, w, Lead.stage == "won", Lead.opened_by != "sheet"),
    }
    items: list[Item] = [
        {"text": f"{stats[key]} {one if stats[key] == 1 else many}", "href": href}
        for key, one, many, href in MOVEMENT_LABELS
        if stats[key]
    ]
    return SectionResult(items, stats, "No pipeline movement in the last 24 hours.")


# ---------------------------------------------------------------------- registration
SECTIONS = (
    DigestSection("follow_ups_due", "Follow-ups due", 25, _follow_ups_due),
    DigestSection("stale_leads", "Stale leads", 26, _stale_leads),
    DigestSection("pipeline_movement", "Pipeline movement", 27, _pipeline_movement),
)


def register() -> None:
    """Register the three CRM sections (idempotent; a second call replaces the first)."""
    for section in SECTIONS:
        digest.register_section(section, replace=True)


__all__ = ["SECTIONS", "register"]

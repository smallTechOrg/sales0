"""Prospect priority (feedback round 2, E4): THE entity carries a priority chip everywhere it is shown.

``prospects.priority`` defaults to the HIGHEST priority of its linked, not-discarded enquiries (``priority_source =
'derived'``, recomputed whenever an enquiry is linked or its priority changes - from the Inbox, triage or a sheet
sync). A person can set it by hand (``priority_source = 'manual'``): a manual value is never overwritten by an
enquiry change or a sheet sync, until "Back to automatic" is chosen (source back to ``derived``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from sales0.db.models import Enquiry, Prospect
from sales0.db.models.enums import PRIORITIES
from sales0.services._common import now_or, not_found
from sales0.services.crm import timeline
from sales0.services.crm._common import invalid
from sales0.services.crm.actor import SYSTEM, TEAM, Actor

RANK = {"high": 3, "medium": 2, "low": 1}
LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
FILTER_CHOICES: tuple[tuple[str, str], ...] = (("high", "High"), ("medium", "Medium"), ("low", "Low"), ("none", "No priority"))


def best_of(values: list[str | None]) -> str | None:
    ranked = [v for v in values if v in RANK]
    return max(ranked, key=lambda v: RANK[v]) if ranked else None


def derived_for(db: Session, prospect_id: int) -> str | None:
    """The highest priority among the prospect's linked enquiries that were not discarded."""
    rows = db.scalars(
        select(Enquiry.priority).where(Enquiry.prospect_id == prospect_id, Enquiry.review_status != "discarded", Enquiry.priority.is_not(None))
    )
    return best_of(list(rows))


def refresh(db: Session, prospect_id: int | None) -> bool:
    """Recompute a derived priority. A manual priority is left alone. True when the value changed."""
    if not prospect_id:
        return False
    p = db.get(Prospect, prospect_id)
    if p is None or p.priority_source == "manual":
        return False
    new = derived_for(db, prospect_id)
    if new is None and p.priority is not None:
        return False  # all linked enquiries lost their priority / were discarded: keep what the team last saw
    if p.priority == new:
        return False
    p.priority = new
    db.flush()
    return True


def refresh_for_enquiry(db: Session, enquiry_id: int) -> None:
    e = db.get(Enquiry, enquiry_id)
    if e is not None and e.prospect_id:
        refresh(db, e.prospect_id)


def check(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text not in PRIORITIES:
        raise invalid("priority_invalid", "Priority must be High, Medium or Low.")
    return text


def set_priority(
    db: Session, prospect_id: int, value: str | None, *, actor: Actor = TEAM, now: datetime | None = None
) -> Prospect:
    """A person sets the priority (source ``manual``). An empty value goes back to the automatic one."""
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise not_found()
    stamp = now_or(now)
    new = check(value)
    before, before_src = p.priority, p.priority_source
    if new is None:
        p.priority_source = "derived"
        p.priority = derived_for(db, prospect_id)
    else:
        p.priority, p.priority_source = new, "manual"
    if (p.priority, p.priority_source) == (before, before_src):
        return p
    p.updated_at = stamp
    db.flush()
    body = (f"Priority set to {LABELS[new]}" if new else "Priority back to automatic"
            + (f" ({LABELS[p.priority]})" if p.priority else ""))
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="note", body=body, actor=actor, occurred_at=stamp,
        data={"system": True, "priority": {"from": before, "to": p.priority, "source": p.priority_source}},
    )
    return p


def rank_order(column: ColumnElement) -> ColumnElement:  # type: ignore[type-arg]
    """ORDER BY key: high first, then medium, low, none."""
    return case((column == "high", 0), (column == "medium", 1), (column == "low", 2), else_=3)


def filter_clause(column: ColumnElement, value: str | None) -> ColumnElement | None:  # type: ignore[type-arg]
    if value == "none":
        return column.is_(None)
    if value in RANK:
        return column == value
    return None


__all__ = [
    "FILTER_CHOICES", "LABELS", "RANK", "SYSTEM", "best_of", "check", "derived_for", "filter_clause", "rank_order", "refresh",
    "refresh_for_enquiry", "set_priority",
]

"""Enquiry deal status: where one specific requirement (an accepted enquiry) stands.

Vocabulary = the Active Enquiries sheet's: open, contacted, follow_up_1, follow_up_2, final_follow_up (mirror the
company's outreach + follow-up cadence; a person may also set them on the ORIGIN enquiry, see below), meeting_scheduled,
proposal_sent, won, lost, no_response, on_hold (set by a person or by the sheet). "Hot / Warm" are the priority tag.

ORIGIN COUPLING (owner decision): the ORIGIN enquiry of a company (its earliest accepted enquiry, i.e. the one whose
acceptance brought the company in) can be set to contacted / follow_up_1 / follow_up_2 / final_follow_up; that moves the
company's prospect sub-status to the same stage (``_drive_company``). Only while the company is an active prospect
(stage prospect, no off-ramp). By hand the move is exact (also backwards); any other source only moves forward, never
past what outreach already recorded. Other enquiries of the company never drive the stage. The reverse direction (the
company's cadence mirrored onto its OPEN / driven enquiries) is done by the projection with Core SQL and never calls back
here, so there is no ping-pong. The sheet reaches the same rule through ``sheetsync.mapping`` (it moves the prospect of
an ORIGIN enquiry row only, and never a stage set in the app).

Side effects (a person confirms, nothing moves a company to Lead by itself): meeting_scheduled / proposal_sent SUGGEST
moving the company to Lead; won marks the company Client (it stays a client; later enquiries are upsell).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.company_projection import DRIVEN_BY_SUB
from sales0.db.models import Company, Enquiry
from sales0.db.models.enums_crm import DRIVEN_DEAL_STATUSES, ENQUIRY_DEAL_STATUSES, HAND_DEAL_STATUSES, PROSPECT_STAGES
from sales0.errors import DomainError
from sales0.services._common import now_or, not_found
from sales0.services.crm import lifecycle, timeline
from sales0.services.crm._common import invalid
from sales0.services.crm.actor import TEAM, Actor

DEAL_LABELS = {
    "open": "Open", "contacted": "Contacted", "follow_up_1": "Follow-up 1", "follow_up_2": "Follow-up 2",
    "final_follow_up": "Final follow-up", "meeting_scheduled": "Meeting scheduled", "proposal_sent": "Proposal sent",
    "won": "Won", "lost": "Lost", "no_response": "No response", "on_hold": "On hold",
}
# what a person (or the sheet) can set by hand; "open" means "follow the company's cadence again"
SETTABLE = ("open", *HAND_DEAL_STATUSES)
BOARD_ORDER = ENQUIRY_DEAL_STATUSES
# the statuses of the origin enquiry that drive the company's prospect sub-status
STAGE_DRIVING = ("contacted", "follow_up_1", "follow_up_2", "final_follow_up")


def deal_label(status: str | None) -> str:
    return DEAL_LABELS.get(status or "", "Not accepted yet")


def origin_enquiry_id(db: Session, company_id: int | None) -> int | None:
    """The enquiry that brought the company in: its earliest accepted enquiry (lowest id)."""
    if company_id is None:
        return None
    return db.scalar(select(func.min(Enquiry.id)).where(Enquiry.company_id == company_id, Enquiry.review_status == "accepted"))


def is_origin(db: Session, enquiry: Enquiry) -> bool:
    return enquiry.review_status == "accepted" and origin_enquiry_id(db, enquiry.company_id) == enquiry.id


def settable_for(db: Session, enquiry: Enquiry) -> tuple[str, ...]:
    """What a person may choose for this enquiry: the origin enquiry also gets the stage-driving statuses."""
    if is_origin(db, enquiry):
        return ("open", *STAGE_DRIVING, *HAND_DEAL_STATUSES)
    return SETTABLE


def _drive_company(db: Session, e: Enquiry, status: str, *, actor: Actor, source: str, stamp: datetime, note: str | None) -> Enquiry:
    """Origin enquiry -> contacted / follow-up N: move the company's prospect sub-status to the same stage."""
    co = db.get(Company, e.company_id) if e.company_id else None
    if co is None or co.stage != "prospect" or co.off_ramp:
        raise DomainError("stage_invalid", "Contacted and follow-up statuses move the company only while it is an active prospect.", 409)
    current = co.sub_status if co.sub_status in PROSPECT_STAGES else "new"
    if source != "app" and PROSPECT_STAGES.index(status) <= PROSPECT_STAGES.index(current):
        return e  # a non-hand source never moves a stage backwards (or sideways) past what outreach recorded
    prev = e.deal_status  # read before the move: the projection mirrors the new cadence onto driven enquiries
    if status != current:
        lifecycle.set_prospect_status(db, co.id, status, actor=actor, note=note or "Set from the enquiry status", now=stamp)
    e.deal_status, e.deal_status_source = status, "driven"  # it now follows the cadence like every driven enquiry
    db.flush()
    db.refresh(co)
    if prev != status:
        timeline.add_event(
            db, subject_type="enquiry", subject_id=e.id, kind="stage_change", enquiry_id=e.id, company_id=e.company_id,
            prospect_id=e.prospect_id, actor=actor, occurred_at=stamp,
            body=f"Enquiry status: {deal_label(prev)} -> {deal_label(status)} (the company's prospect stage follows)",
            data={"from": prev, "to": status, "source": source, "company_sub_status": co.sub_status},
        )
    return e


def set_status(db: Session, enquiry_id: int, status: str, *, actor: Actor = TEAM, source: str = "app", note: str | None = None,
               now: datetime | None = None) -> Enquiry:
    if status not in ENQUIRY_DEAL_STATUSES:
        raise invalid("status_invalid", "Choose a valid enquiry status.")
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    if e.review_status != "accepted":
        raise DomainError("enquiry_not_accepted", "Accept the enquiry in the Inbox before giving it a status.", 409)
    stamp = now_or(now)
    if status in DRIVEN_DEAL_STATUSES and status != "open":
        if not is_origin(db, e):
            raise DomainError(
                "status_driven", "Contacted and follow-up statuses are set on the enquiry that brought the company in, or by "
                "logging the contact / follow-up on the company.", 409)
        return _drive_company(db, e, status, actor=actor, source=source if source in ("app", "sheet") else "app", stamp=stamp, note=note)
    prev = e.deal_status
    if status == "open":  # back to following the company's cadence
        co = db.get(Company, e.company_id) if e.company_id else None
        status = DRIVEN_BY_SUB.get(co.sub_status or "", "open") if co is not None and co.stage == "prospect" and not co.off_ramp else "open"
        e.deal_status_source = "driven"
    else:
        e.deal_status_source = source if source in ("app", "sheet") else "app"
    if status == prev:
        return e
    e.deal_status = status
    db.flush()
    timeline.add_event(
        db, subject_type="enquiry", subject_id=e.id, kind="stage_change", enquiry_id=e.id, company_id=e.company_id,
        prospect_id=e.prospect_id, actor=actor, occurred_at=stamp,
        body=f"Enquiry status: {deal_label(prev)} -> {deal_label(status)}" + (f" - {note.strip()}" if note and note.strip() else ""),
        data={"from": prev, "to": status, "source": e.deal_status_source},
    )
    if e.company_id is not None:
        if status in ("meeting_scheduled", "proposal_sent"):
            lifecycle.suggest_lead(db, e.company_id, "enquiry", f"enquiry {e.id} {status}", now=stamp)
        elif status == "won":
            lifecycle.mark_client(db, e.company_id, actor=actor, note="An enquiry was marked Won", now=stamp)
    return e

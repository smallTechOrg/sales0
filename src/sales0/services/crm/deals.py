"""Enquiry deal status: where one specific requirement (an accepted enquiry) stands.

Vocabulary = the Active Enquiries sheet's: open, contacted, follow_up_1, follow_up_2, final_follow_up (DRIVEN by the
company's outreach + follow-up cadence, never set by hand), meeting_scheduled, proposal_sent, won, lost, no_response,
on_hold (set by a person or by the sheet). "Hot / Warm" are the priority tag, not a status.

Side effects (a person confirms, nothing moves a company by itself): meeting_scheduled / proposal_sent SUGGEST moving the
company to Lead; won marks the company Client (it stays a client; later enquiries are upsell).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from sales0.db.company_projection import DRIVEN_BY_SUB
from sales0.db.models import Company, Enquiry
from sales0.db.models.enums_crm import DRIVEN_DEAL_STATUSES, ENQUIRY_DEAL_STATUSES, HAND_DEAL_STATUSES
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


def deal_label(status: str | None) -> str:
    return DEAL_LABELS.get(status or "", "Not accepted yet")


def set_status(db: Session, enquiry_id: int, status: str, *, actor: Actor = TEAM, source: str = "app", note: str | None = None,
               now: datetime | None = None) -> Enquiry:
    if status not in ENQUIRY_DEAL_STATUSES:
        raise invalid("status_invalid", "Choose a valid enquiry status.")
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    if e.review_status != "accepted":
        raise DomainError("enquiry_not_accepted", "Accept the enquiry in the Inbox before giving it a status.", 409)
    if status in DRIVEN_DEAL_STATUSES and status != "open":
        raise DomainError(
            "status_driven", "Contacted and follow-up statuses follow the company's outreach - log the contact or the "
            "follow-up on the company instead.", 409)
    stamp = now_or(now)
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

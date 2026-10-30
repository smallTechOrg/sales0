"""Enquiry deal status: where one specific requirement (an accepted enquiry) stands.

ONE list for every enquiry, set by a person (Enquiries board, company page) or by the Active Enquiries sheet: open,
contacted, follow_up_1, follow_up_2, final_follow_up, meeting_scheduled, proposal_sent, won, lost, no_response,
on_hold. "Hot / Warm" are the priority tag. The enquiry status and the company's (prospect) status are DIFFERENT things.

Link 1 (owner decision), enquiry -> company, for ANY accepted enquiry of the company:
* contacted / follow_up_1 / follow_up_2 / final_follow_up: while the company is a NEW prospect (stage prospect, no
  off-ramp) the prospect sub-status moves to the same stage, FORWARD ONLY (already at or past it: nothing happens).
* meeting_scheduled / proposal_sent: show "Suggested: Lead"; a person confirms. won: the company becomes a client.
* open / lost / no_response / on_hold: no effect on the company.
A company that is not a prospect (lead, client, off-ramp) or no company at all: the status just changes.

Link 2, prospect -> enquiry: when the prospect itself moves forward to contacted / follow-up 1 / 2 / final (a logged
contact, a sent email, a completed reminder), its accepted enquiries that are still behind that stage follow it, FORWARD
ONLY. Parked or finished enquiry statuses (meeting scheduled .. on hold) are never touched.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Company, Enquiry
from sales0.db.models.enums_crm import ENQUIRY_DEAL_STATUSES, PROSPECT_STAGES
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
SETTABLE = ENQUIRY_DEAL_STATUSES  # the same list for every enquiry
BOARD_ORDER = ENQUIRY_DEAL_STATUSES
# the enquiry statuses that move a NEW prospect company forward to the same stage
STAGE_DRIVING = ("contacted", "follow_up_1", "follow_up_2", "final_follow_up")


def deal_label(status: str | None) -> str:
    return DEAL_LABELS.get(status or "", "Not accepted yet")


def _advance_prospect(db: Session, e: Enquiry, status: str, *, actor: Actor, source: str, stamp: datetime) -> bool:
    """Move the company's prospect sub-status forward to ``status``. True when it moved; no error when it cannot."""
    co = db.get(Company, e.company_id) if e.company_id else None
    if co is None or co.stage != "prospect" or co.off_ramp:
        return False
    if source == "sheet" and co.stage_source == "app":
        return False  # a stage a person set in the app is never moved by the sheet
    current = co.sub_status if co.sub_status in PROSPECT_STAGES else "new"
    if PROSPECT_STAGES.index(status) <= PROSPECT_STAGES.index(current):
        return False  # forward only
    lifecycle.set_prospect_status(db, co.id, status, actor=actor, note="Set from an enquiry status", now=stamp)
    return True


def advance_from_prospect(db: Session, prospect_id: int, stage: str, *, actor: Actor, stamp: datetime) -> int:
    """Move the prospect's accepted enquiries forward to ``stage`` (forward only). Returns how many moved."""
    if stage not in STAGE_DRIVING:
        return 0
    target = STAGE_DRIVING.index(stage)
    moved = 0
    for e in db.scalars(select(Enquiry).where(Enquiry.prospect_id == prospect_id, Enquiry.review_status == "accepted")).all():
        cur = e.deal_status
        if cur in (None, "open"):
            behind = True
        elif cur in STAGE_DRIVING:
            behind = STAGE_DRIVING.index(cur) < target
        else:
            behind = False
        if behind:
            set_status(db, e.id, stage, actor=actor, note="Prospect moved to the same stage", now=stamp)
            moved += 1
    return moved


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
    src = source if source in ("app", "sheet") else "app"
    prev = e.deal_status
    e.deal_status_source = src
    if status == prev:
        return e
    e.deal_status = status
    db.flush()
    moved = status in STAGE_DRIVING and _advance_prospect(db, e, status, actor=actor, source=src, stamp=stamp)
    timeline.add_event(
        db, subject_type="enquiry", subject_id=e.id, kind="stage_change", enquiry_id=e.id, company_id=e.company_id,
        prospect_id=e.prospect_id, actor=actor, occurred_at=stamp,
        body=f"Enquiry status: {deal_label(prev)} -> {deal_label(status)}" + (" (the company's prospect stage follows)" if moved else "")
        + (f" - {note.strip()}" if note and note.strip() else ""),
        data={"from": prev, "to": status, "source": src, "prospect_moved": moved},
    )
    if e.company_id is not None:
        if status in ("meeting_scheduled", "proposal_sent"):
            lifecycle.suggest_lead(db, e.company_id, "enquiry", f"enquiry {e.id} {status}", now=stamp)
        elif status == "won":
            lifecycle.mark_client(db, e.company_id, actor=actor, note="An enquiry was marked Won", now=stamp)
    return e

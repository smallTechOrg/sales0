"""The company lifecycle: the ONE service the UI / routes use to move a company between stages.

A company is the main record; Prospect -> Lead -> Client are its stages and ``not_interested`` / ``dormant`` /
``do_not_contact`` / ``lost_client`` its off-ramps (spec/capabilities/domain-model.md). The stage columns on
``companies`` are a projection of the prospect / lead / client rows (``sales0.db.company_projection``); every action
here goes through the existing ledger services (so follow-up scheduling, tracking, timeline entries and the Won -> Client
path stay exactly as they were) and the projection then updates the company in the same transaction.

Nothing here moves a company by itself: the app only SUGGESTS a lead (``suggest_lead``), a person accepts or dismisses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db import company_projection
from sales0.db.models import Client, Company, Lead, Prospect
from sales0.db.models.enums_crm import (
    COMPANY_ONLY_OFF_RAMPS,
    COMPANY_STAGES,
    COMPANY_SUB_STATUSES,
    OFF_RAMPS,
    PROSPECT_STAGES,
)
from sales0.errors import DomainError
from sales0.services._common import now_or, not_found
from sales0.services.crm import followups, leads, prospects, timeline
from sales0.services.crm._common import invalid
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.stages import stage_rank

STAGE_LABELS = {"prospect": "Prospect", "lead": "Lead", "client": "Client"}
SUB_LABELS = {
    "new": "New", "contacted": "Contacted", "follow_up_1": "Follow-up 1", "follow_up_2": "Follow-up 2",
    "final_follow_up": "Final follow-up", "meeting_scheduled": "Meeting scheduled", "proposal_sent": "Proposal sent",
    "active": "Active",
}
OFF_RAMP_LABELS = {
    "not_interested": "Not interested", "dormant": "Dormant (no response)", "do_not_contact": "Do not contact",
    "lost_client": "Lost client",
}
# off-ramps a person may choose per stage (do_not_contact is always available)
OFF_RAMPS_BY_STAGE = {
    "prospect": ("not_interested", "dormant", "do_not_contact"),
    "lead": ("not_interested", "dormant", "do_not_contact"),
    "client": ("lost_client", "do_not_contact"),
}


def stage_label(stage: str | None) -> str:
    return STAGE_LABELS.get(stage or "", "Not in pipeline")


def sub_label(sub: str | None) -> str:
    return SUB_LABELS.get(sub or "", (sub or "").replace("_", " ").capitalize())


def company_row(db: Session, company_id: int) -> Company:
    row = db.get(Company, company_id)
    if row is None:
        raise not_found()
    return row


def _refresh(db: Session, row: Company) -> None:
    db.flush()
    db.refresh(row)


# ---------------------------------------------------------------------------- ledger access
def ledger_prospects(db: Session, company_id: int) -> list[Prospect]:
    return list(db.scalars(select(Prospect).where(Prospect.company_id == company_id).order_by(Prospect.id)))


def primary_prospect(db: Session, company_id: int) -> Prospect | None:
    """The prospect row company-level actions apply to: the open one furthest along, else a converted one (it has the
    lead), else the most recently closed one."""
    rows = ledger_prospects(db, company_id)
    if not rows:
        return None
    live = [p for p in rows if p.status == "open"]
    if live:
        return max(live, key=lambda p: (stage_rank(p.stage), -p.id))
    conv = [p for p in rows if p.status == "converted"]
    if conv:
        return conv[-1]
    return max(rows, key=lambda p: (p.closed_at or p.updated_at, p.id))


def ensure_ledger(db: Session, company_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> Prospect:
    """The company's primary prospect row, created (stage ``new``) when the company has none yet."""
    p = primary_prospect(db, company_id)
    if p is not None:
        return p
    co = company_row(db, company_id)
    stamp = now_or(now)
    p = Prospect(
        kind="company_enquiry", name=co.name[:200], company_id=co.id, stage="new", stage_source="app", status="open",
        source="manual", actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
    )
    db.add(p)
    db.flush()
    timeline.add_event(db, subject_type="prospect", subject_id=p.id, kind="created", body="Company added to the pipeline.",
                       data={"source": "manual"}, actor=actor, occurred_at=stamp, company_id=co.id, prospect_id=p.id)
    _refresh(db, co)
    return p


def open_lead_of(db: Session, company_id: int) -> Lead | None:
    return db.scalar(
        select(Lead).where(Lead.closed_at.is_(None), (Lead.company_id == company_id) | Lead.prospect_id.in_(
            select(Prospect.id).where(Prospect.company_id == company_id))).order_by(Lead.id.desc()).limit(1)
    )


# ---------------------------------------------------------------------------- guards
def ensure_contactable(db: Session, *, company_id: int | None = None, prospect_id: int | None = None) -> None:
    """Raise when the company is Do not contact: blocks ALL outreach, drafting, sending and follow-up drafting."""
    if company_id is None and prospect_id is not None:
        company_id = db.scalar(select(Prospect.company_id).where(Prospect.id == prospect_id))
    if company_id is None:
        return
    off = db.scalar(select(Company.off_ramp).where(Company.id == company_id))
    if off == "do_not_contact":
        raise DomainError("do_not_contact", "This company is marked Do not contact - nothing can be drafted or sent to it. "
                          "An admin can reopen it.", 409)


# ---------------------------------------------------------------------------- stage actions
def set_prospect_status(db: Session, company_id: int, sub_status: str, *, actor: Actor = TEAM, note: str | None = None,
                        now: datetime | None = None) -> Company:
    """Move a PROSPECT company along the outreach cadence (new .. final follow-up)."""
    if sub_status not in PROSPECT_STAGES:
        raise invalid("stage_invalid", "Choose a valid status.")
    co = company_row(db, company_id)
    if co.stage != "prospect" or co.off_ramp:
        raise DomainError("stage_invalid", "Only an active prospect has an outreach status.", 409)
    ensure_contactable(db, company_id=company_id)
    p = primary_prospect(db, company_id)
    assert p is not None
    prospects.move_stage(db, p.id, sub_status, actor=actor, note=note, now=now)
    _refresh(db, co)
    return co


def add_to_pipeline(db: Session, company_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> Company:
    """A known company (no stage yet) becomes a prospect / new."""
    co = company_row(db, company_id)
    if co.stage is None:
        ensure_ledger(db, company_id, actor=actor, now=now)
        _refresh(db, co)
    return co


def mark_lead(db: Session, company_id: int, *, stage: str = "meeting_scheduled", meeting_at: datetime | None = None,
              actor: Actor = TEAM, note: str | None = None, now: datetime | None = None) -> Company:
    """A PERSON decides: this company is a lead. Overrides everything (no contact / response preconditions); a closed
    company is reopened first. Clears the "suggested: Lead" hint."""
    if stage not in COMPANY_SUB_STATUSES["lead"]:
        raise invalid("stage_invalid", "Choose a valid lead status.")
    co = company_row(db, company_id)
    ensure_contactable(db, company_id=company_id)
    stamp = now_or(now)
    if co.stage == "client" and not co.off_ramp:
        raise DomainError("stage_invalid", "This company is already a client.", 409)
    ld = open_lead_of(db, company_id)
    if ld is not None:
        leads.move_stage(db, ld.id, stage, meeting_at=meeting_at, actor=actor, now=stamp)
    else:
        p = ensure_ledger(db, company_id, actor=actor, now=stamp)
        if p.status == "closed":
            prospects.reopen(db, p.id, actor=actor, now=stamp)
        if co.off_ramp in ("not_interested", "dormant"):
            co.off_ramp = None
        leads.convert(db, p.id, stage=stage, meeting_at=meeting_at, opened_by="manual", actor=actor, note=note, now=stamp,
                      require_contact=False)
    co.lead_suggestion = None
    _refresh(db, co)
    return co


def set_lead_status(db: Session, company_id: int, sub_status: str, *, actor: Actor = TEAM, proposal_sent_on: date | None = None,
                    meeting_at: datetime | None = None, now: datetime | None = None) -> Company:
    if sub_status not in COMPANY_SUB_STATUSES["lead"]:
        raise invalid("stage_invalid", "Choose a valid lead status.")
    co = company_row(db, company_id)
    ld = open_lead_of(db, company_id)
    if ld is None:
        raise DomainError("stage_invalid", "This company is not a lead.", 409)
    leads.move_stage(db, ld.id, sub_status, proposal_sent_on=proposal_sent_on, meeting_at=meeting_at, actor=actor, now=now)
    _refresh(db, co)
    return co


def mark_client(db: Session, company_id: int, *, actor: Actor = TEAM, note: str | None = None, now: datetime | None = None) -> Company:
    """A PERSON marks the company a client (directly, or because an enquiry was marked Won). Idempotent; a company that
    is already a client stays one (later enquiries are upsell)."""
    co = company_row(db, company_id)
    stamp = now_or(now)
    if co.off_ramp in COMPANY_ONLY_OFF_RAMPS:
        if co.off_ramp == "do_not_contact":
            ensure_contactable(db, company_id=company_id)
        co.off_ramp = None  # lost_client -> won back
    already = db.scalar(select(Client.id).where((Client.company_id == company_id) | Client.prospect_id.in_(
        select(Prospect.id).where(Prospect.company_id == company_id))).limit(1))
    if already is None:
        p = ensure_ledger(db, company_id, actor=actor, now=stamp)
        leads.promote_to_client(db, p.id, actor=actor, note=note or "Marked as client", now=stamp)
    co.lead_suggestion = None
    _refresh(db, co)
    return co


def go_off_ramp(db: Session, company_id: int, off_ramp: str, *, reason: str | None = None, actor: Actor = TEAM,
                now: datetime | None = None) -> Company:
    """Close the company: not_interested | dormant | do_not_contact | lost_client. Remembers where it stood so a reopen
    restores it."""
    if off_ramp not in OFF_RAMPS:
        raise invalid("off_ramp_invalid", "Choose a valid way to close this company.")
    co = company_row(db, company_id)
    stamp = now_or(now)
    why = (reason or "").strip()[:200] or None
    if co.off_ramp == off_ramp:
        return co
    if off_ramp == "lost_client" and co.stage != "client":
        raise DomainError("off_ramp_invalid", "Only a client can become a lost client.", 409)
    if off_ramp in ("not_interested", "dormant") and co.stage not in ("prospect", "lead"):
        raise DomainError("off_ramp_invalid", "A client is closed as 'Lost client'.", 409)
    if off_ramp in COMPANY_ONLY_OFF_RAMPS:
        was = (co.stage, co.sub_status, co.off_ramp)
        co.resume_stage, co.resume_sub_status = co.stage, co.sub_status
        co.off_ramp, co.off_ramp_reason, co.off_ramp_at, co.stage_changed_at, co.updated_at = off_ramp, why, stamp, stamp, stamp
        db.flush()
        for p in ledger_prospects(db, company_id):
            followups.cancel_open(db, prospect_id=p.id, reason=off_ramp, actor=actor, now=stamp)
        for ld in db.scalars(select(Lead).where(Lead.closed_at.is_(None), Lead.prospect_id.in_(select(Prospect.id).where(Prospect.company_id == company_id)))):
            followups.cancel_open(db, lead_id=ld.id, reason=off_ramp, actor=actor, now=stamp)
        timeline.add_event(
            db, subject_type="company", subject_id=company_id, kind="stage_change", company_id=company_id, actor=actor,
            body=f"Closed: {OFF_RAMP_LABELS[off_ramp]}" + (f" - {why}" if why else ""),
            data={"off_ramp": off_ramp, "from": {"stage": was[0], "sub_status": was[1], "off_ramp": was[2]}}, occurred_at=stamp,
        )
    else:
        ld = open_lead_of(db, company_id)
        reason_code = "not_interested" if off_ramp == "not_interested" else "dormant"
        if ld is not None:
            leads.move_stage(db, ld.id, "lost" if off_ramp == "not_interested" else "no_response",
                             outcome_note=why, actor=actor, now=stamp)
        else:
            for p in ledger_prospects(db, company_id):
                if p.status == "open":
                    prospects.close(db, p.id, reason_code, actor=actor, now=stamp)
    _refresh(db, co)
    if why and co.off_ramp == off_ramp:
        co.off_ramp_reason = why
    return co


def reopen(db: Session, company_id: int, *, actor: Actor = TEAM, is_admin: bool = False, now: datetime | None = None) -> Company:
    """Restore a closed company to the stage it was in. Do not contact needs an admin."""
    co = company_row(db, company_id)
    stamp = now_or(now)
    off = co.off_ramp
    if off is None:
        return co
    if off == "do_not_contact" and not is_admin:
        raise DomainError("forbidden", "Only an admin can reopen a company marked Do not contact.", 403)
    if off in COMPANY_ONLY_OFF_RAMPS:
        co.off_ramp, co.off_ramp_reason, co.off_ramp_at, co.updated_at = None, None, None, stamp
        db.flush()
        timeline.add_event(db, subject_type="company", subject_id=company_id, kind="stage_change", company_id=company_id,
                           body=f"Reopened (was {OFF_RAMP_LABELS[off]})", data={"reopened": off}, actor=actor, occurred_at=stamp)
        company_projection.recompute(db.connection(), {company_id}, actor=actor.label, actor_member_id=actor.member_id)
        db.refresh(co)
        return co
    resume = co.resume_stage or "prospect"
    if resume == "lead":
        lost = db.scalar(select(Lead).where(Lead.closed_at.is_not(None), Lead.stage.in_(("lost", "no_response")),
                                            (Lead.company_id == company_id) | Lead.prospect_id.in_(select(Prospect.id).where(Prospect.company_id == company_id)))
                         .order_by(Lead.closed_at.desc(), Lead.id.desc()).limit(1))
        sub = co.resume_sub_status if co.resume_sub_status in COMPANY_SUB_STATUSES["lead"] else "meeting_scheduled"
        if lost is not None:
            leads.move_stage(db, lost.id, sub, actor=actor, now=stamp)
    else:
        closed = [p for p in ledger_prospects(db, company_id) if p.status == "closed"]
        for p in sorted(closed, key=lambda r: r.closed_at or r.updated_at, reverse=True)[:1]:
            prospects.reopen(db, p.id, actor=actor, now=stamp)
    _refresh(db, co)
    return co


# ---------------------------------------------------------------------------- suggest Lead
def suggest_lead(db: Session, company_id: int, kind: str, detail: str, *, now: datetime | None = None) -> bool:
    """Show a "suggested: Lead" hint on a prospect company (from a reply classification, a meeting / proposal date, an
    enquiry marked Meeting scheduled / Proposal sent). NEVER moves the stage. A dismissed hint of the same kind and
    detail is not raised again. Returns whether the hint was set."""
    co = db.get(Company, company_id)
    if co is None or co.stage != "prospect" or co.off_ramp in COMPANY_ONLY_OFF_RAMPS:
        return False
    cur = co.lead_suggestion or {}
    if cur and cur.get("kind") == kind and cur.get("detail") == detail:
        return False
    stamp = now_or(now)
    co.lead_suggestion = {"kind": kind, "detail": detail[:200], "at": stamp.astimezone(UTC).isoformat(), "dismissed_at": None}
    db.flush()
    return True


def active_suggestion(co: Company) -> dict[str, Any] | None:
    s = co.lead_suggestion
    if not s or s.get("dismissed_at") or co.stage != "prospect":
        return None
    return s


def dismiss_suggestion(db: Session, company_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> Company:
    co = company_row(db, company_id)
    s = dict(co.lead_suggestion or {})
    if s and not s.get("dismissed_at"):
        s["dismissed_at"] = now_or(now).astimezone(UTC).isoformat()
        co.lead_suggestion = s
        db.flush()
    return co


def accept_suggestion(db: Session, company_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> Company:
    return mark_lead(db, company_id, actor=actor, note="Accepted the suggestion", now=now)


def set_owner(db: Session, company_id: int, member_id: int | None) -> Company:
    co = company_row(db, company_id)
    co.owner_member_id = member_id
    db.flush()
    return co


__all__ = [
    "COMPANY_STAGES", "OFF_RAMP_LABELS", "SUB_LABELS", "SYSTEM", "accept_suggestion", "active_suggestion", "add_to_pipeline",
    "dismiss_suggestion", "ensure_contactable", "ensure_ledger", "go_off_ramp", "mark_client", "mark_lead", "primary_prospect",
    "reopen", "set_lead_status", "set_prospect_status", "suggest_lead",
]

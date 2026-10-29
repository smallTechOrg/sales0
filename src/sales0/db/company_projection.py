"""The company lifecycle (stage / sub-status / off-ramp) as a PROJECTION of the prospect / lead / client rows.

q17 company model: a COMPANY is the main record and Prospect / Lead / Client are stages of it. The historical tables
(``prospects``, ``leads``, ``clients``) stay as the company's outreach + deal ledger (follow-ups, outreach, research,
notes and the timeline hang off them), and the one place that decides what stage a company is in is :func:`derive`.
A Session ``after_flush`` hook re-derives every company whose ledger rows (or accepted enquiries) changed in the flush,
so EVERY writer (services, hooks, sheet sync, importers) keeps ``companies.stage`` / ``sub_status`` / ``off_ramp``
current without knowing about them, and the accepted enquiries' DRIVEN deal statuses follow the company's cadence.

Company-only off-ramps (``do_not_contact``, ``lost_client``) are set by ``services.crm.lifecycle`` and are never
overwritten here. Pure ``derive`` has no I/O (unit-tested); ``recompute`` runs Core SQL on the flush connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from sales0.db.models import Client, Company, Enquiry, Lead, Prospect, TimelineEvent
from sales0.db.models.enums_crm import COMPANY_ONLY_OFF_RAMPS, DRIVEN_DEAL_STATUSES, PROSPECT_STAGES

_PROSPECT_RANK = {s: i for i, s in enumerate(PROSPECT_STAGES)}
_LEAD_RANK = {"meeting_scheduled": 0, "proposal_sent": 1}
STAGE_NAMES = {"prospect": "Prospect", "lead": "Lead", "client": "Client"}
# the closed_reason values that mean "they said no"; anything else (no response, sheet "Hold"...) means dormant
NOT_INTERESTED_REASONS = frozenset({"not_interested", "lost", "enquiry_discarded"})
# company sub-status -> the DRIVEN enquiry deal status
DRIVEN_BY_SUB = {
    "new": "open", "contacted": "contacted", "follow_up_1": "follow_up_1", "follow_up_2": "follow_up_2",
    "final_follow_up": "final_follow_up",
}


@dataclass(frozen=True)
class Lifecycle:
    stage: str | None
    sub_status: str | None
    off_ramp: str | None = None
    resume_stage: str | None = None
    resume_sub_status: str | None = None
    off_ramp_reason: str | None = None
    off_ramp_at: datetime | None = None
    stage_source: str = "app"


@dataclass
class Rows:
    """The ledger rows of one company, as plain dicts (keys: see ``recompute``)."""

    prospects: list[dict[str, Any]] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)
    clients: int = 0


def _off_ramp_for_prospect(reason: str | None) -> str:
    r = (reason or "").strip().lower()
    return "not_interested" if r in NOT_INTERESTED_REASONS else "dormant"


def derive(rows: Rows) -> Lifecycle | None:
    """What the ledger rows say about the company; ``None`` = no ledger row (leave the company as it is)."""
    open_leads = [x for x in rows.leads if x["closed_at"] is None]
    won = any(x["stage"] == "won" for x in rows.leads)
    open_prospects = [p for p in rows.prospects if p["status"] == "open"]
    if rows.clients or won:
        src = "app"
        cl = [x for x in rows.leads if x["stage"] == "won"]
        if cl and all(x["stage_source"] == "sheet" for x in cl) and not any(p["stage_source"] == "app" for p in rows.prospects):
            src = "sheet"
        return Lifecycle("client", "active", stage_source=src)
    if open_leads:
        best = max(open_leads, key=lambda x: _LEAD_RANK.get(x["stage"], 0))
        return Lifecycle("lead", best["stage"], stage_source="app" if any(x["stage_source"] == "app" for x in open_leads) else "sheet")
    if open_prospects:
        best_p = max(open_prospects, key=lambda p: _PROSPECT_RANK.get(p["stage"], 0))
        src = "app" if any(p["stage_source"] == "app" for p in open_prospects) else "sheet"
        return Lifecycle("prospect", best_p["stage"], stage_source=src)
    # nothing live: the company is on an off-ramp - the most recent closure decides which
    closures: list[tuple[datetime, str, str, str, str, str | None]] = []
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for p in rows.prospects:
        if p["status"] == "closed":
            closures.append((p["closed_at"] or p["updated_at"] or epoch, _off_ramp_for_prospect(p["closed_reason"]), "prospect",
                             p["stage"], p["stage_source"], p["closed_reason"]))
    for x in rows.leads:
        if x["closed_at"] is not None and x["stage"] in ("lost", "no_response"):
            sub = "proposal_sent" if x.get("proposal_sent_on") else "meeting_scheduled"
            closures.append((x["closed_at"], "not_interested" if x["stage"] == "lost" else "dormant", "lead", sub,
                             x["stage_source"], x["stage"]))
    if closures:
        at, off, st, sub, src, why = max(closures, key=lambda c: c[0])
        return Lifecycle(st, sub, off_ramp=off, resume_stage=st, resume_sub_status=sub,
                         off_ramp_reason=(why or "").replace("_", " ")[:200] or None, off_ramp_at=at, stage_source=src)
    if rows.prospects:  # converted prospect without a lead row (should not happen): keep it a prospect
        p = rows.prospects[0]
        return Lifecycle("prospect", p["stage"])
    return None


# ---------------------------------------------------------------------------- Core SQL
def _load(conn: Connection, company_id: int) -> Rows:
    pt, lt, ct = Prospect.__table__, Lead.__table__, Client.__table__
    prospects = [
        dict(r._mapping) for r in conn.execute(
            select(pt.c.id, pt.c.stage, pt.c.status, pt.c.closed_reason, pt.c.closed_at, pt.c.stage_source, pt.c.updated_at)
            .where(pt.c.company_id == company_id).order_by(pt.c.id)
        )
    ]
    leads = [
        dict(r._mapping) for r in conn.execute(
            select(lt.c.id, lt.c.stage, lt.c.closed_at, lt.c.stage_source, lt.c.proposal_sent_on)
            .where((lt.c.company_id == company_id) | lt.c.prospect_id.in_(select(pt.c.id).where(pt.c.company_id == company_id)))
        )
    ]
    clients = conn.scalar(
        select(ct.c.id).where((ct.c.company_id == company_id) | ct.c.prospect_id.in_(select(pt.c.id).where(pt.c.company_id == company_id))).limit(1)
    )
    return Rows(prospects=prospects, leads=leads, clients=1 if clients is not None else 0)


def _label(stage: str | None, sub: str | None, off: str | None) -> str:
    base = STAGE_NAMES.get(stage or "", "Not in pipeline")
    if sub:
        base += f" / {sub.replace('_', ' ')}"
    return base + (f" ({off.replace('_', ' ')})" if off else "")


def recompute(conn: Connection, company_ids: set[int], *, actor: str = "system", actor_member_id: int | None = None) -> int:
    """Re-derive the lifecycle of these companies and drive their enquiries' deal statuses. Returns companies changed."""
    ct, et = Company.__table__, Enquiry.__table__
    changed = 0
    now = datetime.now(UTC)
    for cid in sorted(company_ids):
        cur = conn.execute(
            select(ct.c.stage, ct.c.sub_status, ct.c.off_ramp, ct.c.stage_source, ct.c.resume_stage, ct.c.resume_sub_status).where(ct.c.id == cid)
        ).first()
        if cur is None:
            continue
        life = derive(_load(conn, cid))
        stage, sub, off = cur.stage, cur.sub_status, cur.off_ramp
        if life is not None:
            keep_off = cur.off_ramp in COMPANY_ONLY_OFF_RAMPS
            new_off = cur.off_ramp if keep_off else life.off_ramp
            vals: dict[str, Any] = {"stage": life.stage, "sub_status": life.sub_status, "off_ramp": new_off,
                                    "stage_source": life.stage_source}
            if not keep_off:
                vals.update(resume_stage=life.resume_stage, resume_sub_status=life.resume_sub_status,
                            off_ramp_reason=life.off_ramp_reason, off_ramp_at=life.off_ramp_at)
            if (cur.stage, cur.sub_status, cur.off_ramp, cur.stage_source) != (vals["stage"], vals["sub_status"], vals["off_ramp"], vals["stage_source"]):
                vals["stage_changed_at"] = now
                conn.execute(update(ct).where(ct.c.id == cid).values(**vals))
                conn.execute(insert(TimelineEvent.__table__).values(
                    subject_type="company", subject_id=cid, company_id=cid, kind="stage_change",
                    body=f"Company: {_label(cur.stage, cur.sub_status, cur.off_ramp)} -> {_label(life.stage, life.sub_status, new_off)}",
                    data={"from": {"stage": cur.stage, "sub_status": cur.sub_status, "off_ramp": cur.off_ramp},
                          "to": {"stage": life.stage, "sub_status": life.sub_status, "off_ramp": new_off}},
                    actor=actor[:80], actor_member_id=actor_member_id, occurred_at=now, created_at=now,
                ))
                changed += 1
            stage, sub, off = life.stage, life.sub_status, new_off
        # accepted enquiries' DRIVEN deal status mirrors the company's prospect cadence
        if stage == "prospect" and off is None and sub in DRIVEN_BY_SUB:
            target = DRIVEN_BY_SUB[sub]
            conn.execute(
                update(et).where(
                    et.c.company_id == cid, et.c.review_status == "accepted",
                    (et.c.deal_status.is_(None)) | et.c.deal_status.in_(DRIVEN_DEAL_STATUSES),
                    (et.c.deal_status.is_(None)) | (et.c.deal_status != target),
                ).values(deal_status=target, deal_status_source="driven")
            )
        elif et is not None:
            conn.execute(  # accepted but not yet given a status: open
                update(et).where(et.c.company_id == cid, et.c.review_status == "accepted", et.c.deal_status.is_(None))
                .values(deal_status="open", deal_status_source="driven")
            )
    return changed


def _touched(session: Session) -> tuple[set[int], str, int | None]:
    ids: set[int] = set()
    actor, member = "system", None
    pending = list(session.new) + list(session.dirty)
    for obj in pending:
        cid = None
        if isinstance(obj, Prospect | Client):
            cid = obj.company_id
        elif isinstance(obj, Lead):
            cid = obj.company_id
            if cid is None and obj.prospect_id is not None:
                cid = session.connection().scalar(select(Prospect.__table__.c.company_id).where(Prospect.__table__.c.id == obj.prospect_id))
        elif isinstance(obj, Enquiry):
            if obj in session.new or session.is_modified(obj, include_collections=False):
                cid = obj.company_id
        if cid is not None:
            ids.add(cid)
            if isinstance(obj, Prospect | Lead | Client) and getattr(obj, "actor", None):
                actor, member = obj.actor, getattr(obj, "actor_member_id", None)
    return ids, actor, member


_installed = False


def install() -> None:
    """Register the after_flush hook on every Session (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, "after_flush")
    def _after_flush(session: Session, _ctx: Any) -> None:
        if session.info.get("no_company_projection"):
            return
        ids, actor, member = _touched(session)
        if not ids:
            return
        recompute(session.connection(), ids, actor=actor, actor_member_id=member)
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Company) and obj.id in ids:
                session.expire(obj, ["stage", "sub_status", "off_ramp", "off_ramp_reason", "off_ramp_at", "resume_stage",
                                     "resume_sub_status", "stage_source", "stage_changed_at"])
            elif isinstance(obj, Enquiry) and obj.company_id in ids:
                session.expire(obj, ["deal_status", "deal_status_source"])


install()

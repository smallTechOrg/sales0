"""Company model: a COMPANY is the main record, Prospect / Lead / Client are its lifecycle stages.

1. ``companies``: ``stage`` (prospect | lead | client | NULL = known company, not in the pipeline), ``sub_status``,
   ``off_ramp`` (not_interested | dormant | do_not_contact | lost_client), ``off_ramp_reason`` / ``off_ramp_at``,
   ``resume_stage`` / ``resume_sub_status`` (what a reopen restores), ``stage_source`` (app | sheet),
   ``stage_changed_at``, ``lead_suggestion`` (JSON), ``owner_member_id`` (plain integer, NO foreign key: a FK column could not be dropped again
   on SQLite without rebuilding the FK-referenced ``companies``). No CHECK (no rebuild of a FK-referenced table).
2. ``enquiries.deal_status`` / ``deal_status_source``: the deal status of an ACCEPTED enquiry (Active Enquiries sheet
   vocabulary). NULL for pending / discarded ones.
3. BACKFILL (no row is deleted or re-keyed, so every id in a URL survives): every prospect without a company gets one
   (matched by name key, else created from the prospect name: this covers ``outreach_person`` prospects), the same for
   a client without a company; ``leads.company_id`` and the accepted enquiries' ``company_id`` are filled from the
   prospect; each company's lifecycle is derived from its prospect / lead / client rows (client > open lead > open
   prospect > off-ramp from the latest closure) and each accepted enquiry gets a deal status.
   The prospects / leads / clients tables stay as the company's outreach + deal ledger (see
   ``sales0.db.company_projection``, which keeps the company columns current).

Retry-safe: columns/indexes are added only when missing and the backfill is re-runnable (it recomputes). The
downgrade drops the new columns/indexes; the companies it created for companyless prospects/clients stay (the
prospects/clients keep their ``company_id``): data-safe, nothing is deleted.

Revision ID: q17_company_model_c5e9a2d7
Revises: q16_feedback2_a41c7e92
Create Date: 2026-10-29
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "q17_company_model_c5e9a2d7"
down_revision: str | Sequence[str] | None = "q16_feedback2_a41c7e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROSPECT_RANK = {s: i for i, s in enumerate(("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up"))}
_LEAD_RANK = {"meeting_scheduled": 0, "proposal_sent": 1}
_NOT_INTERESTED = {"not_interested", "lost", "enquiry_discarded"}
_DRIVEN = {"new": "open", "contacted": "contacted", "follow_up_1": "follow_up_1", "follow_up_2": "follow_up_2", "final_follow_up": "final_follow_up"}
_WS = re.compile(r"\s+")


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _add(table: str, column: sa.Column, index: str | None = None) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)
    if index and index not in _indexes(table):
        op.create_index(index, table, [column.name])


def _utc(dt: Any) -> Any:
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _key(name: str | None) -> str:
    return _WS.sub(" ", (name or "").strip()).lower()


def _derive(prospects: list[dict], leads: list[dict], clients: int) -> dict | None:
    open_leads = [x for x in leads if x["closed_at"] is None]
    open_p = [p for p in prospects if p["status"] == "open"]
    if clients or any(x["stage"] == "won" for x in leads):
        return {"stage": "client", "sub_status": "active", "stage_source": "app"}
    if open_leads:
        best = max(open_leads, key=lambda x: _LEAD_RANK.get(x["stage"], 0))
        src = "app" if any(x["stage_source"] == "app" for x in open_leads) else "sheet"
        return {"stage": "lead", "sub_status": best["stage"], "stage_source": src}
    if open_p:
        best_p = max(open_p, key=lambda p: _PROSPECT_RANK.get(p["stage"], 0))
        src = "app" if any(p["stage_source"] == "app" for p in open_p) else "sheet"
        return {"stage": "prospect", "sub_status": best_p["stage"], "stage_source": src}
    closures = []
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for p in prospects:
        if p["status"] == "closed":
            off = "not_interested" if (p["closed_reason"] or "").strip().lower() in _NOT_INTERESTED else "dormant"
            closures.append((_utc(p["closed_at"]) or _utc(p["updated_at"]) or epoch, off, "prospect", p["stage"], p["stage_source"], p["closed_reason"]))
    for x in leads:
        if x["closed_at"] is not None and x["stage"] in ("lost", "no_response"):
            sub = "proposal_sent" if x["proposal_sent_on"] else "meeting_scheduled"
            closures.append((_utc(x["closed_at"]) or epoch, "not_interested" if x["stage"] == "lost" else "dormant", "lead", sub, x["stage_source"], x["stage"]))
    if closures:
        at, off, st, sub, src, why = max(closures, key=lambda c: c[0])
        return {"stage": st, "sub_status": sub, "off_ramp": off, "resume_stage": st, "resume_sub_status": sub,
                "off_ramp_reason": (why or "").replace("_", " ")[:200] or None, "off_ramp_at": at, "stage_source": src}
    if prospects:
        return {"stage": "prospect", "sub_status": prospects[0]["stage"], "stage_source": "app"}
    return None


def _backfill() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    meta = sa.MetaData()
    companies = sa.Table("companies", meta, autoload_with=bind)
    prospects = sa.Table("prospects", meta, autoload_with=bind)
    leads = sa.Table("leads", meta, autoload_with=bind)
    clients = sa.Table("clients", meta, autoload_with=bind)
    enquiries = sa.Table("enquiries", meta, autoload_with=bind)

    by_key: dict[str, int] = {k: i for i, k in bind.execute(sa.select(companies.c.id, companies.c.name_key))}

    def company_for(name: str | None) -> int | None:
        clean = _WS.sub(" ", (name or "").strip())[:200]
        if not clean:
            return None
        k = _key(clean)
        if k in by_key:
            return by_key[k]
        cid = bind.execute(sa.insert(companies).values(name=clean, name_key=k, created_at=now, updated_at=now)).inserted_primary_key[0]
        by_key[k] = cid
        return cid

    # 1. companyless prospects / clients get a company; leads and accepted enquiries inherit the prospect's
    for pid, name in bind.execute(sa.select(prospects.c.id, prospects.c.name).where(prospects.c.company_id.is_(None))).all():
        cid = company_for(name) or company_for(f"Unnamed prospect {pid}")
        bind.execute(sa.update(prospects).where(prospects.c.id == pid).values(company_id=cid))
    for kid, name, pid in bind.execute(sa.select(clients.c.id, clients.c.name, clients.c.prospect_id).where(clients.c.company_id.is_(None))).all():
        cid = bind.execute(sa.select(prospects.c.company_id).where(prospects.c.id == pid)).scalar() if pid else None
        cid = cid or company_for(name) or company_for(f"Unnamed client {kid}")
        bind.execute(sa.update(clients).where(clients.c.id == kid).values(company_id=cid))
    bind.execute(
        sa.update(leads).where(leads.c.company_id.is_(None))
        .values(company_id=sa.select(prospects.c.company_id).where(prospects.c.id == leads.c.prospect_id).scalar_subquery())
    )
    bind.execute(
        sa.update(enquiries).where(enquiries.c.company_id.is_(None), enquiries.c.prospect_id.is_not(None))
        .values(company_id=sa.select(prospects.c.company_id).where(prospects.c.id == enquiries.c.prospect_id).scalar_subquery())
    )

    # 2. lifecycle of every company that has ledger rows
    p_by: dict[int, list[dict]] = {}
    for r in bind.execute(sa.select(prospects.c.company_id, prospects.c.stage, prospects.c.status, prospects.c.closed_reason,
                                    prospects.c.closed_at, prospects.c.stage_source, prospects.c.updated_at, prospects.c.id)).all():
        p_by.setdefault(r.company_id, []).append(dict(r._mapping))
    pid_company = {p["id"]: cid for cid, ps in p_by.items() for p in ps}
    l_by: dict[int, list[dict]] = {}
    for r in bind.execute(sa.select(leads.c.company_id, leads.c.prospect_id, leads.c.stage, leads.c.closed_at, leads.c.stage_source, leads.c.proposal_sent_on)).all():
        cid = r.company_id or pid_company.get(r.prospect_id)
        if cid is not None:
            l_by.setdefault(cid, []).append(dict(r._mapping))
    c_count: dict[int, int] = {}
    for r in bind.execute(sa.select(clients.c.company_id, clients.c.prospect_id)).all():
        cid = r.company_id or pid_company.get(r.prospect_id)
        if cid is not None:
            c_count[cid] = c_count.get(cid, 0) + 1
    state: dict[int, dict] = {}
    for cid in set(p_by) | set(l_by) | set(c_count):
        life = _derive(p_by.get(cid, []), l_by.get(cid, []), c_count.get(cid, 0))
        if life is None:
            continue
        state[cid] = life
        vals = {"stage": life["stage"], "sub_status": life["sub_status"], "off_ramp": life.get("off_ramp"),
                "resume_stage": life.get("resume_stage"), "resume_sub_status": life.get("resume_sub_status"),
                "off_ramp_reason": life.get("off_ramp_reason"), "off_ramp_at": life.get("off_ramp_at"),
                "stage_source": life["stage_source"], "stage_changed_at": now}
        bind.execute(sa.update(companies).where(companies.c.id == cid).values(**vals))

    # 3. deal status of every accepted enquiry
    rows = bind.execute(sa.select(enquiries.c.id, enquiries.c.company_id, enquiries.c.prospect_id).where(enquiries.c.review_status == "accepted")).all()
    lead_stage_by_prospect: dict[int, str] = {}
    for r in bind.execute(sa.select(leads.c.prospect_id, leads.c.stage, leads.c.closed_at).order_by(leads.c.id)).all():
        lead_stage_by_prospect[r.prospect_id] = r.stage
    prospect_by_id = {p["id"]: p for ps in p_by.values() for p in ps}
    for eid, cid, pid in rows:
        life = state.get(cid) if cid is not None else None
        status = "open"
        p = prospect_by_id.get(pid) if pid else None
        ls = lead_stage_by_prospect.get(pid) if pid else None
        if ls in ("meeting_scheduled", "proposal_sent", "won", "lost", "no_response"):
            status = ls
        elif p is not None and p["status"] == "closed":
            status = "no_response" if (p["closed_reason"] or "") == "no_response" else ("lost" if (p["closed_reason"] or "") in ("not_interested", "lost") else "on_hold")
        elif p is not None:
            status = _DRIVEN.get(p["stage"], "open")
        elif life is not None and life["stage"] == "prospect":
            status = _DRIVEN.get(life["sub_status"], "open")
        bind.execute(sa.update(enquiries).where(enquiries.c.id == eid).values(deal_status=status, deal_status_source="driven"))


def upgrade() -> None:
    _add("companies", sa.Column("stage", sa.String(10), nullable=True), "ix_companies_stage")
    _add("companies", sa.Column("sub_status", sa.String(20), nullable=True))
    _add("companies", sa.Column("off_ramp", sa.String(20), nullable=True), "ix_companies_off_ramp")
    _add("companies", sa.Column("off_ramp_reason", sa.String(200), nullable=True))
    _add("companies", sa.Column("off_ramp_at", sa.DateTime(timezone=True), nullable=True))
    _add("companies", sa.Column("resume_stage", sa.String(10), nullable=True))
    _add("companies", sa.Column("resume_sub_status", sa.String(20), nullable=True))
    _add("companies", sa.Column("stage_source", sa.String(6), nullable=False, server_default="app"))
    _add("companies", sa.Column("stage_changed_at", sa.DateTime(timezone=True), nullable=True))
    _add("companies", sa.Column("lead_suggestion", sa.JSON(none_as_null=True), nullable=True))
    _add("companies", sa.Column("owner_member_id", sa.Integer(), nullable=True))
    _add("enquiries", sa.Column("deal_status", sa.String(20), nullable=True), "ix_enquiries_deal_status")
    _add("enquiries", sa.Column("deal_status_source", sa.String(6), nullable=True))
    _backfill()


def _drop(table: str, column: str, index: str | None = None) -> None:
    if index and index in _indexes(table):
        op.drop_index(index, table_name=table)
    if column in _columns(table):
        op.drop_column(table, column)


def downgrade() -> None:
    _drop("enquiries", "deal_status_source")
    _drop("enquiries", "deal_status", "ix_enquiries_deal_status")
    for col in ("owner_member_id", "lead_suggestion", "stage_changed_at", "stage_source", "resume_sub_status", "resume_stage",
                "off_ramp_at", "off_ramp_reason"):
        _drop("companies", col)
    _drop("companies", "off_ramp", "ix_companies_off_ramp")
    _drop("companies", "sub_status")
    _drop("companies", "stage", "ix_companies_stage")

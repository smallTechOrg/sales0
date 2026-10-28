"""Clients: created from a won lead (idempotent), or legacy / typed in / imported."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Client, CompetitorMention, Competitor, Lead, Prospect
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import _keys, companies, contacts, timeline
from sales0.services.crm._common import UNSET, invalid, page_params, today_local
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.views import ClientDetail, ClientPage, ClientView, CompetitorView


def view_of(c: Client) -> ClientView:
    return ClientView(id=c.id, name=c.name, company_id=c.company_id, status=c.status, source=c.source,
                      started_on=c.started_on, lead_id=c.lead_id, prospect_id=c.prospect_id)


def _get(db: Session, client_id: int) -> Client:
    row = db.get(Client, client_id)
    if row is None:
        raise not_found()
    return row


def create_from_lead(db: Session, lead_id: int, *, actor: Actor = SYSTEM, now: datetime | None = None) -> ClientView:
    existing = db.scalar(select(Client).where(Client.lead_id == lead_id))
    if existing is not None:
        return view_of(existing)
    ld = db.get(Lead, lead_id)
    if ld is None:
        raise not_found()
    p = db.get(Prospect, ld.prospect_id)
    assert p is not None
    stamp = now_or(now)
    c = Client(
        name=p.name, company_id=ld.company_id or p.company_id, status="active", source="lead", lead_id=ld.id, prospect_id=p.id,
        started_on=today_local(stamp), actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
    )
    db.add(c)
    db.flush()
    if p.contact_id:
        contacts.link(db, p.contact_id, "client", c.id, "buyer")
    timeline.add_event(
        db, subject_type="client", subject_id=c.id, kind="client_created", body=f"Client created from won lead: {c.name}",
        data={"lead_id": ld.id, "prospect_id": p.id}, actor=actor, occurred_at=stamp,
    )
    return view_of(c)


def create_legacy(
    db: Session, *, name: str, company: str | None = None, started_on: date | None = None, notes: str | None = None,
    actor: Actor = TEAM, now: datetime | None = None,
) -> ClientView:
    """A legacy client (older project). Idempotent per (name, company): an existing legacy client is returned."""
    clean = clip(_keys.collapse(name), 200)
    if not clean:
        raise invalid("client_invalid", "A client needs a name.")
    stamp = now_or(now)
    comp = companies.get_or_create_row(db, company, now=stamp)
    stmt = select(Client).where(Client.status == "legacy", func.lower(Client.name) == clean.lower())
    stmt = stmt.where(Client.company_id == comp.id) if comp else stmt.where(Client.company_id.is_(None))
    existing = db.scalar(stmt.order_by(Client.id).limit(1))
    if existing is not None:
        return view_of(existing)
    c = Client(
        name=clean, company_id=comp.id if comp else None, status="legacy",
        source="legacy_import" if actor.label == "import" else "manual", started_on=started_on,
        notes=(notes or "").strip() or None, actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
    )
    db.add(c)
    db.flush()
    timeline.add_event(db, subject_type="client", subject_id=c.id, kind="client_created", body=f"Legacy client added: {c.name}",
                       data={"status": "legacy"}, actor=actor, occurred_at=stamp, company_id=c.company_id)
    return view_of(c)


def get_client(db: Session, client_id: int) -> ClientDetail:
    c = _get(db, client_id)
    if c.prospect_id:
        tl = timeline.list_timeline(db, prospect_id=c.prospect_id)
    else:
        tl = timeline.list_timeline(db, subject=("client", c.id))
    rows = db.execute(
        select(Competitor).join(CompetitorMention, CompetitorMention.competitor_id == Competitor.id)
        .where(CompetitorMention.entity_type == "client", CompetitorMention.entity_id == c.id).order_by(Competitor.name_key)
    ).scalars()
    comps = [
        CompetitorView(id=k.id, name=k.name, website=k.website, approx_clients=k.approx_clients, client_names=k.client_names,
                       contact=k.contact, email=k.email, notes=k.notes, active=k.active, sheet_synced=False, mention_count=0)
        for k in rows
    ]
    return ClientDetail(**view_of(c).model_dump(), notes=c.notes, timeline=tl, competitors=comps)


def update_client(
    db: Session, client_id: int, *, name=UNSET, notes=UNSET, status=UNSET, actor: Actor = TEAM,  # type: ignore[no-untyped-def]
    now: datetime | None = None,
) -> ClientView:
    c = _get(db, client_id)
    changed: list[str] = []
    if name is not UNSET:
        new = clip(_keys.collapse(name), 200)
        if not new:
            raise invalid("client_invalid", "A client needs a name.")
        if new != c.name:
            c.name = new
            changed.append("name")
    if notes is not UNSET and ((notes or "").strip() or None) != c.notes:
        c.notes = (notes or "").strip() or None
        changed.append("notes")
    if status is not UNSET:
        if status not in ("active", "legacy"):
            raise invalid("client_invalid", "A client is active or legacy.")
        if status != c.status:
            c.status = status
            changed.append("status")
    if changed:
        c.updated_at = now_or(now)
        c.actor, c.actor_member_id = actor.label[:80], actor.member_id
        db.flush()
        timeline.add_event(db, subject_type="client", subject_id=c.id, kind="note", body="Details edited: " + ", ".join(changed),
                           data={"system": True, "fields": changed}, actor=actor, occurred_at=now)
    return view_of(c)


def list_clients(db: Session, *, status: str | None = None, q: str | None = None, page: int = 1, page_size: int | None = None) -> ClientPage:
    page, size = page_params(page, page_size)
    stmt = select(Client)
    if status:
        stmt = stmt.where(Client.status == status)
    if q and q.strip():
        stmt = stmt.where(func.lower(Client.name).like(f"%{_keys.like_escape(_keys.name_key(q))}%", escape="\\"))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(stmt.order_by(Client.created_at.desc(), Client.id.desc()).limit(size).offset((page - 1) * size))
    return ClientPage(items=[view_of(r) for r in rows], total=total, page=page, page_size=size)


__all__ = ["create_from_lead", "create_legacy", "get_client", "list_clients", "update_client", "view_of"]

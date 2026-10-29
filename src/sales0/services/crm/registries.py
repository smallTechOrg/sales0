"""Competitors (+ mentions) and channel groups."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import ChannelGroup, Client, Competitor, CompetitorMention, Enquiry, Prospect, SheetSource
from sales0.errors import DomainError
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import _keys, flags, timeline
from sales0.services.crm._common import UNSET, invalid
from sales0.services.crm.actor import TEAM, Actor
from sales0.services.crm.views import ClientView, CompetitorDetail, CompetitorView, GroupView, LinkedEnquiryView

_SHEET_OWNED = "This row comes from the Google Sheet - remove it there (or deactivate it here)."


def _synced_ids(db: Session, target_type: str, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return set(db.scalars(select(SheetSource.target_id).where(SheetSource.target_type == target_type, SheetSource.target_id.in_(ids))))


def _competitor_views(db: Session, rows: list[Competitor]) -> list[CompetitorView]:
    ids = [r.id for r in rows]
    synced = _synced_ids(db, "competitor", ids)
    counts = dict(db.execute(select(CompetitorMention.competitor_id, func.count()).where(CompetitorMention.competitor_id.in_(ids)).group_by(CompetitorMention.competitor_id)).all()) if ids else {}
    return [
        CompetitorView(id=r.id, name=r.name, website=r.website, approx_clients=r.approx_clients, client_names=r.client_names,
                       contact=r.contact, email=r.email, notes=r.notes, active=r.active, sheet_synced=r.id in synced,
                       mention_count=int(counts.get(r.id, 0)))
        for r in rows
    ]


def _get_competitor(db: Session, competitor_id: int) -> Competitor:
    row = db.get(Competitor, competitor_id)
    if row is None:
        raise not_found()
    return row


def list_competitors(db: Session, *, q: str | None = None) -> list[CompetitorView]:
    stmt = select(Competitor)
    if q and q.strip():
        like = f"%{_keys.like_escape(_keys.name_key(q))}%"
        stmt = stmt.where(Competitor.name_key.like(like, escape="\\"))
    return _competitor_views(db, list(db.scalars(stmt.order_by(Competitor.name_key, Competitor.id))))


def get_competitor(db: Session, id: int) -> CompetitorDetail:  # noqa: A002
    row = _get_competitor(db, id)
    base = _competitor_views(db, [row])[0]
    from sales0.services.crm import clients, prospects

    enq_ids = list(db.scalars(select(CompetitorMention.entity_id).where(CompetitorMention.competitor_id == id, CompetitorMention.entity_type == "enquiry")))
    client_ids = list(db.scalars(select(CompetitorMention.entity_id).where(CompetitorMention.competitor_id == id, CompetitorMention.entity_type == "client")))
    enq_rows = list(db.scalars(select(Enquiry).where(Enquiry.id.in_(enq_ids)).order_by(Enquiry.date_added.desc()))) if enq_ids else []
    client_rows = list(db.scalars(select(Client).where(Client.id.in_(client_ids)).order_by(Client.name))) if client_ids else []
    return CompetitorDetail(
        **base.model_dump(),
        enquiries=[prospects.linked_enquiry_view(e) for e in enq_rows],
        clients=[clients.view_of(c) for c in client_rows],
    )


def _fields(name, website, approx_clients, client_names, contact, email, notes) -> dict:  # type: ignore[no-untyped-def]
    out: dict = {}
    if name is not UNSET:
        out["name"] = clip(_keys.collapse(name), 200)
    if website is not UNSET:
        out["website"] = clip(website, 300)
        out["website_key"] = _keys.website_key(website) if website else None
    if approx_clients is not UNSET:
        try:
            out["approx_clients"] = int(approx_clients) if approx_clients not in (None, "") else None
        except (TypeError, ValueError):
            raise invalid("competitor_invalid", "Approx. clients must be a whole number.") from None
    if client_names is not UNSET:
        out["client_names"] = (client_names or "").strip() or None
    if contact is not UNSET:
        out["contact"] = clip(contact, 300)
    if email is not UNSET:
        out["email"] = _keys.first_email(email) if email else None
    if notes is not UNSET:
        out["notes"] = (notes or "").strip() or None
    return out


def _check_unique(db: Session, values: dict, own_id: int | None) -> None:
    if values.get("name"):
        other = db.scalar(select(Competitor.id).where(Competitor.name_key == _keys.name_key(values["name"])))
        if other is not None and other != own_id:
            raise invalid("competitor_invalid", "A competitor with that name already exists.")
    if values.get("website_key"):
        other = db.scalar(select(Competitor.id).where(Competitor.website_key == values["website_key"]))
        if other is not None and other != own_id:
            raise invalid("competitor_invalid", "A competitor with that website already exists.")


def create_competitor(
    db: Session, *, name: str, website=None, approx_clients=None, client_names=None, contact=None, email=None, notes=None,  # type: ignore[no-untyped-def]
    actor: Actor = TEAM, now: datetime | None = None,
) -> CompetitorView:
    values = _fields(name, website, approx_clients, client_names, contact, email, notes)
    if not values.get("name"):
        raise invalid("competitor_invalid", "A competitor needs a name.")
    _check_unique(db, values, None)
    stamp = now_or(now)
    row = Competitor(name_key=_keys.name_key(values["name"]), active=True, created_at=stamp, updated_at=stamp, **values)
    db.add(row)
    db.flush()
    timeline.add_event(db, subject_type="competitor", subject_id=row.id, kind="created", body=f"Competitor added: {row.name}",
                       actor=actor, occurred_at=stamp)
    _rescan_enquiries(db, now=stamp)
    return _competitor_views(db, [row])[0]


def update_competitor(
    db: Session, id: int, *, name=UNSET, website=UNSET, approx_clients=UNSET, client_names=UNSET, contact=UNSET,  # type: ignore[no-untyped-def]  # noqa: A002
    email=UNSET, notes=UNSET, active=UNSET, actor: Actor = TEAM, now: datetime | None = None,
) -> CompetitorView:
    row = _get_competitor(db, id)
    values = _fields(name, website, approx_clients, client_names, contact, email, notes)
    if "name" in values and not values["name"]:
        raise invalid("competitor_invalid", "A competitor needs a name.")
    _check_unique(db, values, row.id)
    changed: list[str] = []
    for k, v in values.items():
        if k == "website_key":
            row.website_key = v
        elif getattr(row, k) != v:
            setattr(row, k, v)
            changed.append(k)
    if "name" in values:
        row.name_key = _keys.name_key(values["name"])
    rescan = "name" in changed or "website" in changed
    if active is not UNSET and bool(active) != row.active:
        row.active = bool(active)
        changed.append("active")
        rescan = True
    if changed:
        row.updated_at = now_or(now)
        db.flush()
        timeline.add_event(db, subject_type="competitor", subject_id=row.id, kind="note", body="Details edited: " + ", ".join(changed),
                           data={"system": True, "fields": changed}, actor=actor, occurred_at=now)
        if rescan:
            _rescan_enquiries(db, now=now)
    return _competitor_views(db, [row])[0]


def _rescan_enquiries(db: Session, *, now: datetime | None) -> None:
    """Competitor list or activity changed: refresh the automatic mention links of every enquiry."""
    flags.recompute_competitor_mentions(db, now=now)


def delete_competitor(db: Session, id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> None:  # noqa: A002
    row = _get_competitor(db, id)
    if id in _synced_ids(db, "competitor", [id]):
        raise DomainError("sheet_owned", _SHEET_OWNED, 409)
    affected = db.execute(
        select(CompetitorMention.entity_type, CompetitorMention.entity_id).where(CompetitorMention.competitor_id == id)
    ).all()
    name = row.name
    db.delete(row)
    db.flush()
    db.expire_all()
    for et, eid in affected:
        flags.sync_competitor_flag(db, et, eid, now=now)
        if et == "enquiry":
            pid = db.scalar(select(Enquiry.prospect_id).where(Enquiry.id == eid))
            if pid:
                flags._sync_prospect_competitor_flag(db, pid, now)  # noqa: SLF001
    timeline.add_event(db, subject_type="competitor", subject_id=id, kind="note", body=f"Competitor deleted: {name}",
                       data={"system": True, "deleted": True}, actor=actor, occurred_at=now)


def link_competitor(db: Session, competitor_id: int, entity_type: str, entity_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> None:
    _get_competitor(db, competitor_id)
    model = {"enquiry": Enquiry, "client": Client, "prospect": Prospect}.get(entity_type)
    if model is None:
        raise invalid("competitor_invalid", "A competitor links to an enquiry, a client or a prospect.")
    if db.get(model, entity_id) is None:
        raise not_found()
    existing = db.scalar(select(CompetitorMention).where(CompetitorMention.competitor_id == competitor_id,
                                                        CompetitorMention.entity_type == entity_type,
                                                        CompetitorMention.entity_id == entity_id))
    if existing is not None:
        if existing.source != "user":
            existing.source = "user"
            db.flush()
        return
    db.add(CompetitorMention(competitor_id=competitor_id, entity_type=entity_type, entity_id=entity_id, source="user",
                             created_at=now_or(now)))
    db.flush()
    flags.sync_competitor_flag(db, entity_type, entity_id, source="user", now=now)
    timeline.add_event(
        db, subject_type="competitor", subject_id=competitor_id, kind="note", body=f"Linked to {entity_type} #{entity_id}",
        data={"system": True, "entity_type": entity_type, "entity_id": entity_id}, actor=actor, occurred_at=now,
    )


# ------------------------------------------------------------------------------ groups
def _keep_level(details: str | None) -> str | None:
    t = (details or "").strip().lower().replace("_", "-").replace(" ", "-")
    if t == "keep":
        return "keep"
    if t in ("keep-low", "keeplow"):
        return "keep_low"
    return None


def _link_key(link: str | None) -> str | None:
    text = (link or "").strip()
    return _keys.website_key(text) or (text.lower() or None)


def _group_view(g: ChannelGroup, synced: bool) -> GroupView:
    return GroupView(id=g.id, name=g.name, platform=g.platform, link=g.link, details=g.details, keep_level=g.keep_level,
                     active=g.active, sheet_synced=synced)


def list_groups(db: Session, *, active: bool | None = None) -> list[GroupView]:
    stmt = select(ChannelGroup)
    if active is not None:
        stmt = stmt.where(ChannelGroup.active.is_(active))
    rows = list(db.scalars(stmt.order_by(ChannelGroup.name_key, ChannelGroup.id)))
    synced = _synced_ids(db, "channel_group", [r.id for r in rows])
    return [_group_view(r, r.id in synced) for r in rows]


def _get_group(db: Session, group_id: int) -> ChannelGroup:
    row = db.get(ChannelGroup, group_id)
    if row is None:
        raise not_found()
    return row


def _check_group_unique(db: Session, platform: str, name_key: str, link_key: str | None, own_id: int | None) -> None:
    if link_key:
        other = db.scalar(select(ChannelGroup.id).where(ChannelGroup.link_key == link_key))
        if other is not None and other != own_id:
            raise invalid("group_invalid", "A group with that link already exists.")
    else:
        other = db.scalar(select(ChannelGroup.id).where(ChannelGroup.platform == platform, ChannelGroup.name_key == name_key, ChannelGroup.link_key.is_(None)))
        if other is not None and other != own_id:
            raise invalid("group_invalid", "A group with that name already exists on this platform.")


def create_group(
    db: Session, *, name: str, platform: str = "whatsapp", link=None, details=None, notes=None,  # type: ignore[no-untyped-def]
    actor: Actor = TEAM, now: datetime | None = None,
) -> GroupView:
    clean = clip(_keys.collapse(name), 200)
    if not clean:
        raise invalid("group_invalid", "A group needs a name.")
    plat = (platform or "whatsapp").strip().lower()[:20] or "whatsapp"
    lk = _link_key(link)
    _check_group_unique(db, plat, _keys.name_key(clean), lk, None)
    stamp = now_or(now)
    row = ChannelGroup(name=clean, name_key=_keys.name_key(clean), platform=plat, link=clip(link, 500), link_key=lk,
                       details=clip(details, 200), keep_level=_keep_level(details), active=True,
                       notes=(notes or "").strip() or None, created_at=stamp, updated_at=stamp)
    db.add(row)
    db.flush()
    timeline.add_event(db, subject_type="group", subject_id=row.id, kind="created", body=f"Group added: {row.name}",
                       actor=actor, occurred_at=stamp)
    return _group_view(row, False)


def update_group(
    db: Session, id: int, *, name=UNSET, platform=UNSET, link=UNSET, details=UNSET, notes=UNSET, active=UNSET,  # type: ignore[no-untyped-def]  # noqa: A002
    actor: Actor = TEAM, now: datetime | None = None,
) -> GroupView:
    row = _get_group(db, id)
    changed: list[str] = []
    new_name = row.name
    if name is not UNSET:
        new_name = clip(_keys.collapse(name), 200) or ""
        if not new_name:
            raise invalid("group_invalid", "A group needs a name.")
    new_platform = row.platform if platform is UNSET else ((platform or "whatsapp").strip().lower()[:20] or "whatsapp")
    new_link = row.link if link is UNSET else clip(link, 500)
    new_key = _link_key(new_link) if link is not UNSET else row.link_key
    _check_group_unique(db, new_platform, _keys.name_key(new_name), new_key, row.id)
    for attr, val in (("name", new_name), ("platform", new_platform), ("link", new_link)):
        if getattr(row, attr) != val:
            setattr(row, attr, val)
            changed.append(attr)
    row.name_key, row.link_key = _keys.name_key(new_name), new_key
    if details is not UNSET:
        d = clip(details, 200)
        if d != row.details:
            row.details, row.keep_level = d, _keep_level(d)
            changed.append("details")
    if notes is not UNSET and ((notes or "").strip() or None) != row.notes:
        row.notes = (notes or "").strip() or None
        changed.append("notes")
    if active is not UNSET and bool(active) != row.active:
        row.active = bool(active)
        changed.append("active")
    if changed:
        row.updated_at = now_or(now)
        db.flush()
        timeline.add_event(db, subject_type="group", subject_id=row.id, kind="note", body="Details edited: " + ", ".join(changed),
                           data={"system": True, "fields": changed}, actor=actor, occurred_at=now)
    return _group_view(row, row.id in _synced_ids(db, "channel_group", [row.id]))


def delete_group(db: Session, id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> None:  # noqa: A002
    row = _get_group(db, id)
    if id in _synced_ids(db, "channel_group", [id]):
        raise DomainError("sheet_owned", _SHEET_OWNED, 409)
    name = row.name
    db.delete(row)
    db.flush()
    timeline.add_event(db, subject_type="group", subject_id=id, kind="note", body=f"Group deleted: {name}",
                       data={"system": True, "deleted": True}, actor=actor, occurred_at=now)


__all__ = [
    "ClientView", "LinkedEnquiryView", "create_competitor", "create_group", "delete_competitor", "delete_group", "get_competitor",
    "link_competitor", "list_competitors", "list_groups", "update_competitor", "update_group",
]

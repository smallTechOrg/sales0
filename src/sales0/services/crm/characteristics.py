"""Characteristics of THE entity (Training provider, Partner, Outreach client, ...): an editable registry plus
multi-valued assignments.

Anchor rule (binding): a prospect IS the entity, so its characteristics are stored on the prospect; a lead and a
client that came from a prospect read (and edit) the prospect's set, so nothing is lost on Prospect -> Lead ->
Client. Only a client WITHOUT a prospect (legacy / typed in) stores its own (``entity_type = 'client'``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from sales0.db.models import Characteristic, Client, EntityCharacteristic, Lead, Prospect
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import _keys, timeline
from sales0.services.crm._common import UNSET, invalid
from sales0.services.crm.actor import TEAM, Actor
from sales0.services.crm.views import CharacteristicView

MAX_NAME = 60

#: AI triage hints -> the seeded characteristic (by ``system_key``) the Accept form pre-ticks. A suggestion only:
#: the human sees it ticked and decides.
_POSTER_HINTS = {"training_agency": "training_provider"}
_OPPORTUNITY_HINTS = {"agency_requirement": "training_provider"}


def suggested_keys(poster_type: str | None, opportunity_type: str | None) -> set[str]:
    out: set[str] = set()
    if poster_type in _POSTER_HINTS:
        out.add(_POSTER_HINTS[poster_type])
    if opportunity_type in _OPPORTUNITY_HINTS:
        out.add(_OPPORTUNITY_HINTS[opportunity_type])
    return out


def _view(c: Characteristic, count: int = 0) -> CharacteristicView:
    return CharacteristicView(id=c.id, name=c.name, system_key=c.system_key, active=c.active, count=count)


# ---------------------------------------------------------------------------- registry
def list_characteristics(db: Session, *, active_only: bool = False, with_counts: bool = False) -> list[CharacteristicView]:
    stmt = select(Characteristic)
    if active_only:
        stmt = stmt.where(Characteristic.active.is_(True))
    rows = list(db.scalars(stmt.order_by(Characteristic.sort_order, Characteristic.name_key, Characteristic.id)))
    counts: dict[int, int] = {}
    if with_counts and rows:
        counts = dict(
            db.execute(
                select(EntityCharacteristic.characteristic_id, func.count()).group_by(EntityCharacteristic.characteristic_id)
            ).all()
        )
    return [_view(r, int(counts.get(r.id, 0))) for r in rows]


def _get(db: Session, characteristic_id: int) -> Characteristic:
    row = db.get(Characteristic, characteristic_id)
    if row is None:
        raise not_found()
    return row


def _clean_name(name: str | None) -> str:
    clean = clip(_keys.collapse(name), MAX_NAME) or ""
    if not clean:
        raise invalid("characteristic_invalid", "A characteristic needs a name.")
    return clean


def _check_unique(db: Session, name: str, own_id: int | None) -> None:
    other = db.scalar(select(Characteristic.id).where(Characteristic.name_key == _keys.name_key(name)))
    if other is not None and other != own_id:
        raise invalid("characteristic_invalid", "A characteristic with that name already exists.")


def create(db: Session, *, name: str, now: datetime | None = None) -> CharacteristicView:
    clean = _clean_name(name)
    _check_unique(db, clean, None)
    order = int(db.scalar(select(func.max(Characteristic.sort_order))) or 0) + 1
    stamp = now_or(now)
    row = Characteristic(name=clean, name_key=_keys.name_key(clean), active=True, sort_order=order, created_at=stamp, updated_at=stamp)
    db.add(row)
    db.flush()
    return _view(row)


def update(db: Session, characteristic_id: int, *, name=UNSET, active=UNSET, now: datetime | None = None) -> CharacteristicView:  # type: ignore[no-untyped-def]
    row = _get(db, characteristic_id)
    changed = False
    if name is not UNSET:
        clean = _clean_name(name)
        _check_unique(db, clean, row.id)
        if clean != row.name:
            row.name, row.name_key, changed = clean, _keys.name_key(clean), True
    if active is not UNSET and bool(active) != row.active:
        row.active, changed = bool(active), True
    if changed:
        row.updated_at = now_or(now)
        db.flush()
    return _view(row)


# ---------------------------------------------------------------------------- anchors
def anchor_of(db: Session, entity_type: str, entity_id: int) -> tuple[str, int]:
    """The (entity_type, entity_id) the characteristics of a prospect / lead / client live on."""
    if entity_type == "prospect":
        if db.get(Prospect, entity_id) is None:
            raise not_found()
        return "prospect", entity_id
    if entity_type == "lead":
        ld = db.get(Lead, entity_id)
        if ld is None:
            raise not_found()
        return "prospect", ld.prospect_id
    if entity_type == "client":
        c = db.get(Client, entity_id)
        if c is None:
            raise not_found()
        return ("prospect", c.prospect_id) if c.prospect_id else ("client", c.id)
    raise invalid("characteristic_invalid", "Characteristics belong to a prospect, a lead or a client.")


def _rows_for(db: Session, entity_type: str, ids: Iterable[int]) -> dict[int, list[CharacteristicView]]:
    ids = list({i for i in ids if i})
    out: dict[int, list[CharacteristicView]] = defaultdict(list)
    if not ids:
        return out
    for eid, c in db.execute(
        select(EntityCharacteristic.entity_id, Characteristic)
        .join(Characteristic, Characteristic.id == EntityCharacteristic.characteristic_id)
        .where(EntityCharacteristic.entity_type == entity_type, EntityCharacteristic.entity_id.in_(ids))
        .order_by(Characteristic.sort_order, Characteristic.name_key)
    ).all():
        out[eid].append(_view(c))
    return out


def for_prospects(db: Session, prospect_ids: Iterable[int]) -> dict[int, list[CharacteristicView]]:
    return _rows_for(db, "prospect", prospect_ids)


def for_clients(db: Session, rows: Iterable[Client]) -> dict[int, list[CharacteristicView]]:
    rows = list(rows)
    by_prospect = for_prospects(db, [r.prospect_id for r in rows if r.prospect_id])
    own = _rows_for(db, "client", [r.id for r in rows if not r.prospect_id])
    return {r.id: (by_prospect.get(r.prospect_id, []) if r.prospect_id else own.get(r.id, [])) for r in rows}


def of(db: Session, entity_type: str, entity_id: int) -> list[CharacteristicView]:
    kind, anchor = anchor_of(db, entity_type, entity_id)
    return _rows_for(db, kind, [anchor]).get(anchor, [])


# ---------------------------------------------------------------------------- assignment
def _event(db: Session, kind: str, anchor: int, added: list[str], removed: list[str], actor: Actor, now: datetime | None) -> None:
    parts = ([f"+ {', '.join(added)}"] if added else []) + ([f"- {', '.join(removed)}"] if removed else [])
    timeline.add_event(
        db, subject_type=kind, subject_id=anchor, kind="note", body="Characteristics: " + "; ".join(parts),
        data={"system": True, "characteristics_added": added, "characteristics_removed": removed}, actor=actor, occurred_at=now,
    )


def _apply(
    db: Session, entity_type: str, entity_id: int, wanted: set[int], *, replace: bool, actor: Actor, now: datetime | None,
) -> list[CharacteristicView]:
    kind, anchor = anchor_of(db, entity_type, entity_id)
    current = {
        r.characteristic_id: r
        for r in db.scalars(select(EntityCharacteristic).where(EntityCharacteristic.entity_type == kind, EntityCharacteristic.entity_id == anchor))
    }
    registry = {c.id: c for c in db.scalars(select(Characteristic).where(Characteristic.id.in_(wanted | set(current))))} if (wanted or current) else {}
    unknown = wanted - set(registry)
    if unknown:
        raise invalid("characteristic_invalid", "That characteristic no longer exists - refresh the page.")
    stamp = now_or(now)
    added: list[str] = []
    for cid in sorted(wanted - set(current), key=lambda i: (registry[i].sort_order, registry[i].name_key)):
        if not registry[cid].active:
            raise invalid("characteristic_invalid", f"'{registry[cid].name}' is no longer in use - choose another characteristic.")
        db.add(EntityCharacteristic(characteristic_id=cid, entity_type=kind, entity_id=anchor, actor=actor.label[:80], created_at=stamp))
        added.append(registry[cid].name)
    removed: list[str] = []
    if replace:
        for cid in set(current) - wanted:
            db.delete(current[cid])
            removed.append(registry[cid].name)
    if added or removed:
        db.flush()
        _event(db, kind, anchor, added, sorted(removed), actor, stamp)
    return _rows_for(db, kind, [anchor]).get(anchor, [])


def set_for(
    db: Session, entity_type: str, entity_id: int, characteristic_ids: Iterable[int], *, actor: Actor = TEAM,
    now: datetime | None = None,
) -> list[CharacteristicView]:
    """Replace the set of the entity behind (``entity_type``, ``entity_id``) with ``characteristic_ids``."""
    return _apply(db, entity_type, entity_id, {int(i) for i in characteristic_ids}, replace=True, actor=actor, now=now)


def add_to(
    db: Session, entity_type: str, entity_id: int, characteristic_ids: Iterable[int], *, actor: Actor = TEAM,
    now: datetime | None = None,
) -> list[CharacteristicView]:
    """Add ``characteristic_ids`` (keeps the ones already set)."""
    return _apply(db, entity_type, entity_id, {int(i) for i in characteristic_ids}, replace=False, actor=actor, now=now)


def move_client_rows_to_prospect(db: Session, client: Client) -> None:
    """A client that gains a prospect: its own rows join the prospect's set (no duplicates)."""
    if not client.prospect_id:
        return
    have = set(db.scalars(select(EntityCharacteristic.characteristic_id).where(
        EntityCharacteristic.entity_type == "prospect", EntityCharacteristic.entity_id == client.prospect_id)))
    for row in list(db.scalars(select(EntityCharacteristic).where(EntityCharacteristic.entity_type == "client", EntityCharacteristic.entity_id == client.id))):
        if row.characteristic_id in have:
            db.delete(row)
        else:
            row.entity_type, row.entity_id = "prospect", client.prospect_id
    db.flush()


# ---------------------------------------------------------------------------- filters
def _has(entity_type: str, entity_id: ColumnElement, characteristic_id: int) -> ColumnElement:
    return exists().where(
        EntityCharacteristic.entity_type == entity_type,
        EntityCharacteristic.entity_id == entity_id,
        EntityCharacteristic.characteristic_id == characteristic_id,
    )


def prospect_filter(characteristic_id: int, prospect_id_col: ColumnElement | None = None) -> ColumnElement:
    """WHERE clause: the prospect (``Prospect.id`` or a ``prospect_id`` column) has the characteristic."""
    return _has("prospect", Prospect.id if prospect_id_col is None else prospect_id_col, characteristic_id)


def client_filter(characteristic_id: int) -> ColumnElement:
    return or_(
        and_(Client.prospect_id.is_not(None), _has("prospect", Client.prospect_id, characteristic_id)),
        and_(Client.prospect_id.is_(None), _has("client", Client.id, characteristic_id)),
    )


__all__ = [
    "add_to", "anchor_of", "client_filter", "create", "for_clients", "for_prospects", "list_characteristics",
    "move_client_rows_to_prospect", "of", "prospect_filter", "set_for", "suggested_keys", "update",
]

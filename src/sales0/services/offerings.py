"""Offerings catalogue CRUD (capability search-setup)."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, Keyword, Offering, OfferingGroup
from sales0.db.models._types import name_key
from sales0.errors import DomainError
from sales0.services._common import not_found
from sales0.services.views import OfferingInput, OfferingView

INVALID_MESSAGE = "Offering name (1–80) and group (1–60) are required."


def _validate(data: OfferingInput) -> OfferingInput:
    name = (data.name or "").strip()
    group = (data.group_name or "").strip()
    desc = (data.description or "").strip()
    if not (1 <= len(name) <= 80) or not (1 <= len(group) <= 60) or len(desc) > 500:
        raise DomainError("offering_invalid", INVALID_MESSAGE, 400)
    return OfferingInput(name=name, group_name=group, description=desc)


def _usage_counts(db: Session, offering_ids: list[int] | None = None) -> dict[int, int]:
    """Enquiries referencing each offering (editable value OR the AI's original pick)."""
    counts: dict[int, int] = {}
    stmt = select(Offering.id, func.count(func.distinct(Enquiry.id))).join(
        Enquiry, or_(Enquiry.offering_id == Offering.id, Enquiry.ai_offering_id == Offering.id)
    )
    if offering_ids is not None:
        stmt = stmt.where(Offering.id.in_(offering_ids))
    for oid, n in db.execute(stmt.group_by(Offering.id)).all():
        counts[int(oid)] = int(n)
    return counts


def _view(row: Offering, count: int) -> OfferingView:
    return OfferingView(
        id=row.id,
        name=row.name,
        group_id=row.group_id,
        group_name=row.group.name,
        description=row.description or "",
        active=row.active,
        sort_order=row.sort_order,
        enquiry_count=count,
    )


def _one_view(db: Session, row: Offering) -> OfferingView:
    return _view(row, _usage_counts(db, [row.id]).get(row.id, 0))


def list_offerings(db: Session, include_inactive: bool = False) -> list[OfferingView]:
    stmt = (
        select(Offering)
        .join(OfferingGroup, OfferingGroup.id == Offering.group_id)
        .order_by(OfferingGroup.sort_order, OfferingGroup.id, Offering.sort_order, Offering.id)
    )
    if not include_inactive:
        stmt = stmt.where(Offering.active.is_(True))
    rows = db.scalars(stmt).all()
    counts = _usage_counts(db)
    return [_view(r, counts.get(r.id, 0)) for r in rows]


def list_group_names(db: Session) -> list[str]:
    """Names of groups that hold at least one active offering (empty groups are hidden)."""
    stmt = (
        select(OfferingGroup.name)
        .join(Offering, Offering.group_id == OfferingGroup.id)
        .where(Offering.active.is_(True))
        .group_by(OfferingGroup.id, OfferingGroup.name, OfferingGroup.sort_order)
        .order_by(OfferingGroup.sort_order, OfferingGroup.id)
    )
    return [str(n) for n in db.scalars(stmt).all()]


def _group_for(db: Session, group_name: str) -> OfferingGroup:
    group = db.scalar(select(OfferingGroup).where(OfferingGroup.name_key == name_key(group_name)))
    if group is None:
        next_order = (db.scalar(select(func.max(OfferingGroup.sort_order))) or 0) + 1
        group = OfferingGroup(name=group_name, sort_order=next_order)
        db.add(group)
        db.flush()
    return group


def _next_sort_order(db: Session, group_id: int) -> int:
    return (db.scalar(select(func.max(Offering.sort_order)).where(Offering.group_id == group_id)) or 0) + 1


def create_offering(db: Session, data: OfferingInput) -> OfferingView:
    data = _validate(data)
    existing = db.scalar(select(Offering).where(Offering.name_key == name_key(data.name)))
    if existing is not None and existing.active:
        raise DomainError("offering_duplicate", f"An offering named '{existing.name}' already exists.", 409)
    group = _group_for(db, data.group_name)
    if existing is not None:
        # A deactivated offering with this name is brought back instead of duplicated.
        existing.name = data.name
        existing.description = data.description
        if existing.group_id != group.id:
            existing.group_id = group.id
            existing.sort_order = _next_sort_order(db, group.id)
        existing.active = True
        db.flush()
        db.refresh(existing)
        return _one_view(db, existing)
    row = Offering(
        name=data.name,
        description=data.description,
        group_id=group.id,
        active=True,
        sort_order=_next_sort_order(db, group.id),
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return _one_view(db, row)


def update_offering(db: Session, offering_id: int, data: OfferingInput) -> OfferingView:
    row = db.get(Offering, offering_id)
    if row is None:
        raise not_found()
    data = _validate(data)
    clash = db.scalar(select(Offering).where(Offering.name_key == name_key(data.name), Offering.id != offering_id))
    if clash is not None:
        raise DomainError("offering_duplicate", f"An offering named '{clash.name}' already exists.", 409)
    group = _group_for(db, data.group_name)
    row.name = data.name
    row.description = data.description
    if row.group_id != group.id:
        row.group_id = group.id
        row.sort_order = _next_sort_order(db, group.id)
    db.flush()
    # Keywords tied to this offering follow a rename.
    db.execute(update(Keyword).where(Keyword.offering_id == row.id).values(group_name=data.name[:40]))
    db.flush()
    db.expire_all()
    return _one_view(db, row)


def set_offering_active(db: Session, offering_id: int, active: bool) -> OfferingView:
    """Switch an offering on/off. Off hides it from triage and the inbox selects and pauses its keywords."""
    row = db.get(Offering, offering_id)
    if row is None:
        raise not_found()
    row.active = bool(active)
    db.flush()
    db.refresh(row)
    return _one_view(db, row)


def delete_offering(db: Session, offering_id: int) -> Literal["deleted", "deactivated"]:
    """Hard-delete an unused offering; deactivate one any enquiry references."""
    row = db.get(Offering, offering_id)
    if row is None:
        raise not_found()
    if _usage_counts(db, [offering_id]).get(offering_id, 0) > 0:
        row.active = False
        db.flush()
        return "deactivated"
    db.execute(update(Keyword).where(Keyword.offering_id == offering_id).values(offering_id=None))
    db.delete(row)
    db.flush()
    db.expire_all()
    return "deleted"


__all__ = [
    "create_offering",
    "delete_offering",
    "list_group_names",
    "list_offerings",
    "set_offering_active",
    "update_offering",
]

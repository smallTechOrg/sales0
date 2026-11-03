"""Company offerings: the offerings a company is a fit for (several allowed). Set by a person (company page) or by the
AI on research / refresh when the company has none yet (``source = 'ai'``). Used by the Companies and Pipeline filters."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sales0.db.models import Company, CompanyOffering, Offering
from sales0.services._common import not_found


def ids_for(db: Session, company_id: int) -> list[int]:
    return [int(i) for i in db.scalars(select(CompanyOffering.offering_id).where(CompanyOffering.company_id == company_id)
                                        .order_by(CompanyOffering.id))]


def names_by_company(db: Session, company_ids: list[int]) -> dict[int, list[str]]:
    """Per company: its offerings' names (catalogue order)."""
    out: dict[int, list[str]] = {}
    if not company_ids:
        return out
    rows = db.execute(
        select(CompanyOffering.company_id, Offering.name).join(Offering, Offering.id == CompanyOffering.offering_id)
        .where(CompanyOffering.company_id.in_(company_ids)).order_by(Offering.sort_order, Offering.id)
    ).all()
    for cid, name in rows:
        out.setdefault(int(cid), []).append(name)
    return out


def companies_with(offering_id: int):  # type: ignore[no-untyped-def]
    """Subquery of company ids tagged with ``offering_id`` (the filters)."""
    return select(CompanyOffering.company_id).where(CompanyOffering.offering_id == offering_id)


def set_for(db: Session, company_id: int, offering_ids: Iterable[int], *, source: str = "manual") -> list[int]:
    """Replace the company's offerings with ``offering_ids`` (unknown ids are ignored)."""
    if db.get(Company, company_id) is None:
        raise not_found()
    wanted = [int(i) for i in dict.fromkeys(offering_ids)]
    valid = set(db.scalars(select(Offering.id).where(Offering.id.in_(wanted or [-1]))))
    db.execute(delete(CompanyOffering).where(CompanyOffering.company_id == company_id))
    for oid in wanted:
        if oid in valid:
            db.add(CompanyOffering(company_id=company_id, offering_id=oid, source=source))
    db.flush()
    return [i for i in wanted if i in valid]


def add_if_untagged(db: Session, company_id: int, names: Iterable[str]) -> list[str]:
    """The AI's offerings, only when the company has none yet (a person's choice is never overwritten). Matches active
    offerings by name (case-insensitive). Returns the names it tagged."""
    if ids_for(db, company_id):
        return []
    by_name = {o.name.strip().lower(): o for o in db.scalars(select(Offering).where(Offering.active.is_(True)))}
    picked = [by_name[n.strip().lower()] for n in names if n.strip().lower() in by_name]
    picked = list({o.id: o for o in picked}.values())
    for o in picked:
        db.add(CompanyOffering(company_id=company_id, offering_id=o.id, source="ai"))
    db.flush()
    return [o.name for o in picked]


__all__ = ["add_if_untagged", "companies_with", "ids_for", "names_by_company", "set_for"]

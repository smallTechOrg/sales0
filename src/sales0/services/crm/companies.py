"""Companies: get-or-create by name key, detail with prospects/enquiries/contacts/timeline."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Company, Contact, Enquiry, Prospect
from sales0.services._common import now_or, not_found
from sales0.services.crm import _keys
from sales0.services.crm._common import invalid, page_params
from sales0.services.crm.views import CompanyDetail, CompanyPage, CompanyView


def _view(c: Company) -> CompanyView:
    return CompanyView(id=c.id, name=c.name, website=c.website, city=c.city)


def get_or_create_row(
    db: Session, name: str | None, *, website: str | None = None, city: str | None = None, now: datetime | None = None
) -> Company | None:
    """The ORM row for ``name`` (created if new); None for a blank name. Fills empty website/city."""
    clean = _keys.collapse(name)[:200]
    if not clean:
        return None
    key = _keys.name_key(clean)
    row = db.scalar(select(Company).where(Company.name_key == key))
    web = _keys.collapse(website)[:300] or None
    web_key = _keys.website_key(web)
    town = _keys.collapse(city)[:100] or None
    if row is None:
        row = Company(name=clean, name_key=key, website=web, website_key=web_key, city=town,
                      created_at=now_or(now), updated_at=now_or(now))
        db.add(row)
        db.flush()
        return row
    changed = False
    if web and not row.website:
        row.website, row.website_key, changed = web, web_key, True
    if town and not row.city:
        row.city, changed = town, True
    if changed:
        row.updated_at = now_or(now)
        db.flush()
    return row


def get_or_create(db: Session, name: str, *, website: str | None = None, city: str | None = None) -> CompanyView:
    row = get_or_create_row(db, name, website=website, city=city)
    if row is None:
        raise invalid("company_invalid", "A company needs a name.")
    return _view(row)


def get_company(db: Session, company_id: int) -> CompanyDetail:
    from sales0.services.crm import contacts, prospects, timeline

    c = db.get(Company, company_id)
    if c is None:
        raise not_found()
    prospect_rows = list(db.scalars(select(Prospect).where(Prospect.company_id == c.id).order_by(Prospect.updated_at.desc(), Prospect.id.desc())))
    enquiry_rows = list(db.scalars(select(Enquiry).where(Enquiry.company_id == c.id).order_by(Enquiry.date_added.desc(), Enquiry.id.desc())))
    contact_rows = list(db.scalars(select(Contact).where(Contact.company_id == c.id).order_by(Contact.id).limit(200)))
    return CompanyDetail(
        **_view(c).model_dump(),
        prospects=prospects.views_for(db, prospect_rows),
        enquiries=[prospects.linked_enquiry_view(e) for e in enquiry_rows],
        contacts=contacts.views_for(db, contact_rows),
        timeline=timeline.list_timeline(db, company_id=c.id),
    )


def list_companies(db: Session, *, q: str | None = None, page: int = 1, page_size: int | None = None) -> CompanyPage:
    page, size = page_params(page, page_size)
    stmt = select(Company)
    if q and q.strip():
        stmt = stmt.where(Company.name_key.like(f"%{_keys.like_escape(_keys.name_key(q))}%", escape="\\"))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(stmt.order_by(Company.name_key, Company.id).limit(size).offset((page - 1) * size))
    return CompanyPage(items=[_view(r) for r in rows], total=total, page=page, page_size=size)


__all__ = ["get_company", "get_or_create", "get_or_create_row", "list_companies"]

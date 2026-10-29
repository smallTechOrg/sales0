"""Pipeline read models for the company model, one per Pipeline tab: Enquiries (accepted enquiries grouped by deal
status), Prospects (companies in stage prospect: New .. Final follow-up, a recent "Became lead" lane, a collapsed Closed
lane), Leads (Meeting scheduled | Proposal sent, a recent "Won" lane) and Clients (a list). A company is in exactly one
tab: Leads and Clients never appear in the Prospects columns."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Client, Company, Contact, Enquiry, Flag, Lead, Prospect, TeamMember
from sales0.db.models.enums_crm import COMPANY_STAGES, COMPANY_SUB_STATUSES, ENQUIRY_DEAL_STATUSES, PROSPECT_STAGES
from sales0.services.crm import _keys, characteristics, companies, deals, lifecycle, priority
from sales0.services.crm._common import page_params
from sales0.services.crm.views import CharacteristicView

COLUMN_LABELS = {"prospect": "Prospect", "lead": "Lead", "client": "Client", "closed": "Closed"}
RECENT_DAYS = 30  # the "Became lead" / "Won" lanes only show what happened in the last 30 days
BECAME_LEAD = "became_lead"
WON = "won"
# deal statuses that live in the collapsed lane of the Enquiries tab
ENQUIRY_CLOSED_STATUSES = ("lost", "no_response", "on_hold")
ENQUIRY_ACTIVE_STATUSES = tuple(x for x in ENQUIRY_DEAL_STATUSES if x not in ENQUIRY_CLOSED_STATUSES)


class CompanyCard(BaseModel):
    id: int
    name: str
    city: str | None = None
    stage: str | None = None
    stage_label: str
    sub_status: str | None = None
    sub_label: str | None = None
    off_ramp: str | None = None
    off_ramp_label: str | None = None
    priority: str | None = None
    priority_source: str = "derived"
    characteristics: list[CharacteristicView] = Field(default_factory=list)
    owner: str | None = None
    next_follow_up_on: date | None = None
    suggested_lead: bool = False
    enquiries: int = 0
    categories: list[str] = Field(default_factory=list)


class CompanyColumn(BaseModel):
    key: str
    label: str
    total: int
    cards: list[CompanyCard]


class CompanyBoard(BaseModel):
    columns: list[CompanyColumn]


class CompanyListPage(BaseModel):
    items: list[CompanyCard]
    total: int
    page: int
    page_size: int


class EnquiryCard(BaseModel):
    id: int
    summary: str | None = None
    company_id: int | None = None
    company_name: str | None = None
    deal_status: str | None = None
    priority: str | None = None
    category: str = "relevant_prospect"
    date_added: object | None = None
    source_url: str | None = None
    contact_id: int | None = None
    person_name: str | None = None  # always shown: the contact on the enquiry, else the contact person / post author
    person_title: str | None = None
    company_card: CompanyCard | None = None  # stage, owner, characteristics, next follow-up of the company (same as its card)


class EnquiryColumn(BaseModel):
    key: str
    label: str
    total: int
    cards: list[EnquiryCard]


class EnquiryBoard(BaseModel):
    columns: list[EnquiryColumn]


# ---------------------------------------------------------------------------- filters
def _filtered(stmt, *, q, characteristic_id, priority_filter, category, flag):  # type: ignore[no-untyped-def]
    if q and q.strip():
        like = f"%{_keys.like_escape(_keys.name_key(q))}%"
        stmt = stmt.where(or_(Company.name_key.like(like, escape="\\"),
                              Company.id.in_(select(Prospect.company_id).where(func.lower(Prospect.name).like(like, escape="\\")))))
    if priority_filter:
        clause = priority.filter_clause(Prospect.priority, priority_filter)
        if clause is not None:
            stmt = stmt.where(Company.id.in_(select(Prospect.company_id).where(clause)))
    if category in ("channel_partner", "competitor", "relevant_prospect"):
        stmt = stmt.where(exists().where(Enquiry.company_id == Company.id, Enquiry.category == category, Enquiry.review_status != "discarded"))
    if characteristic_id:
        stmt = stmt.where(Company.id.in_(companies_with_trait(characteristic_id)))
    if flag:
        stmt = stmt.where(Company.id.in_(select(Prospect.company_id).where(
            exists().where(Flag.entity_type == "prospect", Flag.entity_id == Prospect.id, Flag.kind == flag, Flag.cleared_at.is_(None)))))
    return stmt


def companies_with_trait(characteristic_id: int):  # type: ignore[no-untyped-def]
    from sales0.db.models import Client

    return select(Prospect.company_id).where(characteristics.prospect_filter(characteristic_id)).union(
        select(Client.company_id).where(characteristics.client_filter(characteristic_id))
    )


def _order(sort: str | None):  # type: ignore[no-untyped-def]
    best = select(func.min(case_rank())).where(Prospect.company_id == Company.id).correlate(Company).scalar_subquery()
    if sort == "priority":
        return (best, Company.updated_at.desc(), Company.id.desc())
    return (Company.updated_at.desc(), Company.id.desc())


def case_rank():  # type: ignore[no-untyped-def]
    return priority.rank_order(Prospect.priority)


# ---------------------------------------------------------------------------- cards
def cards_for(db: Session, rows: list[Company]) -> list[CompanyCard]:
    ids = [r.id for r in rows]
    traits = companies._characteristics_by_company(db, ids)  # noqa: SLF001
    nxt: dict[int, date] = {}
    best: dict[int, tuple[str, str]] = {}
    if ids:
        for cid, due in db.execute(
            select(Prospect.company_id, func.min(Prospect.next_follow_up_on)).where(Prospect.company_id.in_(ids)).group_by(Prospect.company_id)
        ).all():
            if due is not None:
                nxt[cid] = due
        for cid, prio, src in db.execute(
            select(Prospect.company_id, Prospect.priority, Prospect.priority_source)
            .where(Prospect.company_id.in_(ids), Prospect.priority.is_not(None))
        ).all():
            if prio in priority.RANK and priority.RANK[prio] > priority.RANK.get(best.get(cid, ("", ""))[0], 0):
                best[cid] = (prio, src)
    counts = dict(db.execute(select(Enquiry.company_id, func.count()).where(
        Enquiry.company_id.in_(ids or [-1]), Enquiry.review_status == "accepted").group_by(Enquiry.company_id)).all()) if ids else {}
    cats: dict[int, set[str]] = {}
    if ids:
        for cid, cat in db.execute(select(Enquiry.company_id, Enquiry.category).where(
                Enquiry.company_id.in_(ids), Enquiry.review_status != "discarded", Enquiry.category != "relevant_prospect").distinct()).all():
            cats.setdefault(cid, set()).add(cat)
    owners = {}
    oids = {r.owner_member_id for r in rows if r.owner_member_id}
    if oids:
        owners = dict(db.execute(select(TeamMember.id, TeamMember.name).where(TeamMember.id.in_(oids))).all())
    out = []
    for r in rows:
        prio = best.get(r.id)
        out.append(CompanyCard(
            id=r.id, name=r.name, city=r.city, stage=r.stage, stage_label=lifecycle.stage_label(r.stage), sub_status=r.sub_status,
            sub_label=lifecycle.sub_label(r.sub_status) if r.sub_status else None, off_ramp=r.off_ramp,
            off_ramp_label=lifecycle.OFF_RAMP_LABELS.get(r.off_ramp or ""), priority=prio[0] if prio else None,
            priority_source=prio[1] if prio else "derived", characteristics=traits.get(r.id, []), owner=owners.get(r.owner_member_id),
            next_follow_up_on=nxt.get(r.id), suggested_lead=lifecycle.active_suggestion(r) is not None,
            enquiries=int(counts.get(r.id, 0)), categories=sorted(cats.get(r.id, set())),
        ))
    return out


# ---------------------------------------------------------------------------- reads
def _recent_ids(model, cutoff: datetime):  # type: ignore[no-untyped-def]
    """Company ids that have a ``model`` (Lead / Client) ledger row created since ``cutoff``."""
    return select(model.company_id).where(model.company_id.is_not(None), model.created_at >= cutoff).union(
        select(Prospect.company_id).join(model, model.prospect_id == Prospect.id).where(
            Prospect.company_id.is_not(None), model.created_at >= cutoff))


def _column(db: Session, key: str, label: str, stmt, f: dict, sort: str | None, per_column: int) -> CompanyColumn:  # type: ignore[no-untyped-def]
    stmt = _filtered(stmt, **f)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(*_order(sort)).limit(per_column)))
    return CompanyColumn(key=key, label=label, total=total, cards=cards_for(db, rows))


def _stage_columns(stage: str, subs: tuple[str, ...]):  # type: ignore[no-untyped-def]
    for sub in subs:
        yield sub, lifecycle.sub_label(sub), select(Company).where(Company.stage == stage, Company.off_ramp.is_(None), Company.sub_status == sub)


def tab_board(db: Session, tab: str, *, now: datetime | None = None, q: str | None = None, characteristic_id: int | None = None,
              priority_filter: str | None = None, category: str | None = None, flag: str | None = None, sort: str | None = None,
              per_column: int = 50) -> CompanyBoard:
    """The board of the Prospects or Leads tab. Prospects: New .. Final follow-up, ``became_lead`` (stage lead, a lead
    row created in the last 30 days), ``closed``. Leads: Meeting scheduled, Proposal sent, ``won`` (stage client, a
    client row created in the last 30 days), ``closed``."""
    f = dict(q=q, characteristic_id=characteristic_id, priority_filter=priority_filter, category=category, flag=flag)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=RECENT_DAYS)
    cols = [_column(db, k, label, st, f, sort, per_column)
            for k, label, st in _stage_columns("prospect" if tab == "prospects" else "lead",
                                               PROSPECT_STAGES if tab == "prospects" else COMPANY_SUB_STATUSES["lead"])]
    if tab == "prospects":
        cols.append(_column(db, BECAME_LEAD, "Became lead", select(Company).where(
            Company.stage == "lead", Company.off_ramp.is_(None), Company.id.in_(_recent_ids(Lead, cutoff))), f, sort, per_column))
        closed_stage = "prospect"
    else:
        cols.append(_column(db, WON, "Won (last 30 days)", select(Company).where(
            Company.stage == "client", Company.off_ramp.is_(None), Company.id.in_(_recent_ids(Client, cutoff))), f, sort, per_column))
        closed_stage = "lead"
    cols.append(_column(db, "closed", "Closed", select(Company).where(Company.stage == closed_stage, Company.off_ramp.is_not(None)),
                        f, sort, per_column))
    return CompanyBoard(columns=cols)


def company_list(db: Session, *, stage: str | None = None, sub_status: str | None = None, closed: bool | None = False,
                 q: str | None = None, characteristic_id: int | None = None, priority_filter: str | None = None,
                 category: str | None = None, flag: str | None = None, sort: str | None = None, page: int = 1,
                 page_size: int | None = None) -> CompanyListPage:
    page, size = page_params(page, page_size)
    stmt = select(Company).where(Company.stage.is_not(None))
    if stage in COMPANY_STAGES:
        stmt = stmt.where(Company.stage == stage)
    if sub_status and any(sub_status in v for v in COMPANY_SUB_STATUSES.values()):
        stmt = stmt.where(Company.sub_status == sub_status)
    if closed is True:
        stmt = stmt.where(Company.off_ramp.is_not(None))
    elif closed is False:
        stmt = stmt.where(Company.off_ramp.is_(None))
    stmt = _filtered(stmt, q=q, characteristic_id=characteristic_id, priority_filter=priority_filter, category=category, flag=flag)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(*_order(sort)).limit(size).offset((page - 1) * size)))
    return CompanyListPage(items=cards_for(db, rows), total=total, page=page, page_size=size)


def counts(db: Session) -> dict[str, int]:
    """Active companies per stage (the filter pills) and the closed total."""
    out = {s: 0 for s in COMPANY_STAGES}
    for stage, n in db.execute(select(Company.stage, func.count()).where(Company.stage.is_not(None), Company.off_ramp.is_(None)).group_by(Company.stage)).all():
        out[stage] = int(n)
    out["enquiries"] = int(db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.review_status == "accepted")) or 0)
    out["closed"] = int(db.scalar(select(func.count()).select_from(Company).where(Company.off_ramp.is_not(None), Company.stage.is_not(None))) or 0)
    return out


def enquiry_board(db: Session, *, q: str | None = None, characteristic_id: int | None = None, priority_filter: str | None = None,
                  category: str | None = None, per_column: int = 50) -> EnquiryBoard:
    cols: list[EnquiryColumn] = []
    for status in (*ENQUIRY_ACTIVE_STATUSES, *ENQUIRY_CLOSED_STATUSES):
        stmt = select(Enquiry, Company.name, Contact.full_name, Contact.title).outerjoin(Company, Company.id == Enquiry.company_id).outerjoin(
            Contact, Contact.id == Enquiry.contact_id).where(Enquiry.review_status == "accepted", Enquiry.deal_status == status)
        if q and q.strip():
            like = f"%{_keys.like_escape(_keys.name_key(q))}%"
            stmt = stmt.where(or_(func.lower(func.coalesce(Company.name, "")).like(like, escape="\\"),
                                  func.lower(func.coalesce(Enquiry.requirement_summary, "")).like(like, escape="\\")))
        clause = priority.filter_clause(Enquiry.priority, priority_filter) if priority_filter else None
        if clause is not None:
            stmt = stmt.where(clause)
        if category in ("channel_partner", "competitor", "relevant_prospect"):
            stmt = stmt.where(Enquiry.category == category)
        if characteristic_id:
            stmt = stmt.where(Enquiry.company_id.in_(companies_with_trait(characteristic_id)))
        total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = db.execute(stmt.order_by(priority.rank_order(Enquiry.priority), Enquiry.date_added.desc(), Enquiry.id.desc()).limit(per_column)).all()
        company_ids = {e.company_id for e, *_ in rows if e.company_id}
        by_company = {c.id: c for c in cards_for(db, list(db.scalars(select(Company).where(Company.id.in_(company_ids))))) } if company_ids else {}
        cols.append(EnquiryColumn(key=status, label=deals.deal_label(status), total=total, cards=[
            EnquiryCard(id=e.id, summary=e.requirement_summary, company_id=e.company_id, company_name=name or e.company,
                        deal_status=e.deal_status, priority=e.priority, category=e.category or "relevant_prospect",
                        date_added=e.date_added, source_url=e.source_url,
                        contact_id=e.contact_id, person_name=cname or e.contact_person or e.author_name,
                        person_title=ctitle or (e.author_headline if not cname and not e.contact_person else None),
                        company_card=by_company.get(e.company_id))
            for e, name, cname, ctitle in rows
        ]))
    return EnquiryBoard(columns=cols)


__all__ = ["CompanyBoard", "CompanyCard", "EnquiryBoard", "cards_for", "company_list", "counts", "enquiry_board", "tab_board"]

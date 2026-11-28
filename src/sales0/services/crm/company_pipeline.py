"""Pipeline read models for the company model, one per Pipeline board. A company is only ever on ONE board (q24):

* Enquiries (SMA-33: the ENQUIRY path): accepted enquiries whose company came in through an enquiry and is still a
  prospect (New, Contacted, Follow-up, Final follow-up), or has no company / is not in the pipeline yet;
* Prospects (the OUTREACH path: partnership or a proposal of ours): New, Contacted, Follow-up, Final follow-up;
* Leads (SMA-17): Qualified, Engaged, On hold (Won -> Customers; Lost / No response -> closed);
* Clients: a list.
Closed companies (an off-ramp) are on no board: they stay under Companies with a Closed tag (the Archived view)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.orm import Session

from sales0.db.models import Company, Contact, Enquiry, Flag, Prospect, TeamMember
from sales0.db.models.enums_crm import COMPANY_STAGES, COMPANY_SUB_STATUSES, ENQUIRY_DEAL_STATUSES, PROSPECT_STAGES
from sales0.services.crm import _keys, characteristics, companies, deals, lifecycle, priority
from sales0.services.crm._common import page_params
from sales0.services.crm.views import CharacteristicView

COLUMN_LABELS = {"prospect": "Prospect", "lead": "Lead", "client": "Customer", "closed": "Closed"}
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
    contacts: list[str] = Field(default_factory=list)  # up to CARD_CONTACTS names, the prospect's main contact first
    contacts_more: int = 0  # how many more people the company has
    offerings: list[str] = Field(default_factory=list)  # q23: the offerings it is a fit for
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
    author_profile_url: str | None = None  # SMA-59: the LinkedIn author's profile, shown on the card
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


# ---------------------------------------------------------------------------- which board
def _has_accepted_enquiry():  # type: ignore[no-untyped-def]
    return exists().where(Enquiry.company_id == Company.id, Enquiry.review_status == "accepted")


def on_enquiries_board():  # type: ignore[no-untyped-def]
    """SMA-33: a prospect company on the ENQUIRY path (it came in through an accepted enquiry) stays on the Enquiries
    board through New, Contacted, Follow-up and Final follow-up; one on the OUTREACH path is on the Prospects board."""
    return and_(Company.stage == "prospect", Company.off_ramp.is_(None), func.coalesce(Company.pipeline_path, "enquiry") == "enquiry",
                _has_accepted_enquiry())


def board_of(stage: str | None, sub_status: str | None, off_ramp: str | None, has_enquiry: bool, path: str | None = None) -> str | None:
    """The one board a company is on (None = closed / not in the pipeline)."""
    if off_ramp or stage is None:
        return None
    if stage == "prospect":
        return "enquiries" if has_enquiry and (path or "enquiry") == "enquiry" else "prospects"
    return {"lead": "leads", "client": "clients"}.get(stage)


# ---------------------------------------------------------------------------- filters
def _filtered(stmt, *, q, characteristic_id, priority_filter, category, flag, offering_id=None):  # type: ignore[no-untyped-def]
    if offering_id:
        from sales0.services.crm import company_offerings

        stmt = stmt.where(Company.id.in_(company_offerings.companies_with(offering_id)))
    if q and q.strip():
        like = f"%{_keys.like_escape(_keys.name_key(q))}%"
        stmt = stmt.where(or_(Company.name_key.like(like, escape="\\"),
                              Company.id.in_(select(Prospect.company_id).where(func.lower(Prospect.name).like(like, escape="\\")))))
    if priority_filter:
        clause = priority.filter_clause(priority.of_company(Company.id), priority_filter)
        if clause is not None:
            stmt = stmt.where(clause)
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


def _order(sort: str | None = None):  # type: ignore[no-untyped-def]
    """SMA-57: highest priority first on every board and list, then the most recently changed."""
    return (priority.rank_order(priority.of_company(Company.id)), Company.updated_at.desc(), Company.id.desc())


# ---------------------------------------------------------------------------- cards
CARD_CONTACTS = 3


def _contact_names(db: Session, ids: list[int]) -> dict[int, list[str]]:
    """Per company: its people's names (merged duplicates left out), each prospect's main contact first."""
    if not ids:
        return {}
    main = {int(c) for c in db.scalars(select(Prospect.contact_id).where(Prospect.company_id.in_(ids), Prospect.contact_id.is_not(None)))}
    rows = db.execute(
        select(Contact.company_id, Contact.id, Contact.full_name)
        .where(Contact.company_id.in_(ids), Contact.merged_into_id.is_(None), Contact.full_name.is_not(None))
        .order_by(Contact.id)
    ).all()
    out: dict[int, list[tuple[bool, int, str]]] = {}
    for cid, contact_id, name in rows:
        text = " ".join((name or "").split())
        if text:
            out.setdefault(int(cid), []).append((contact_id not in main, int(contact_id), text))
    return {cid: list(dict.fromkeys(n for _m, _i, n in sorted(items))) for cid, items in out.items()}


def cards_for(db: Session, rows: list[Company]) -> list[CompanyCard]:
    ids = [r.id for r in rows]
    people = _contact_names(db, ids)
    from sales0.services.crm import company_offerings

    offer_names = company_offerings.names_by_company(db, ids)
    traits = companies._characteristics_by_company(db, ids)  # noqa: SLF001
    nxt: dict[int, date] = {}
    best: dict[int, tuple[str | None, str]] = {}
    if ids:
        for cid, due in db.execute(
            select(Prospect.company_id, func.min(Prospect.next_follow_up_on)).where(Prospect.company_id.in_(ids)).group_by(Prospect.company_id)
        ).all():
            if due is not None:
                nxt[cid] = due
        for cid, prio, src in db.execute(
            select(Company.id, priority.of_company(Company.id), priority.source_of_company(Company.id)).where(Company.id.in_(ids))
        ).all():
            best[cid] = (prio, src or "derived")
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
        prio = best.get(r.id) or (None, "derived")
        out.append(CompanyCard(
            id=r.id, name=r.name, city=r.city, stage=r.stage, stage_label=lifecycle.stage_label(r.stage), sub_status=r.sub_status,
            sub_label=lifecycle.sub_label(r.sub_status) if r.sub_status else None, off_ramp=r.off_ramp,
            off_ramp_label=lifecycle.OFF_RAMP_LABELS.get(r.off_ramp or ""), priority=prio[0],
            priority_source=prio[1], characteristics=traits.get(r.id, []), owner=owners.get(r.owner_member_id),
            next_follow_up_on=nxt.get(r.id), suggested_lead=lifecycle.active_suggestion(r) is not None,
            enquiries=int(counts.get(r.id, 0)), categories=sorted(cats.get(r.id, set())),
            offerings=offer_names.get(r.id, []), contacts=people.get(r.id, [])[:CARD_CONTACTS], contacts_more=max(len(people.get(r.id, [])) - CARD_CONTACTS, 0),
        ))
    return out


# ---------------------------------------------------------------------------- reads
def _column(db: Session, key: str, label: str, stmt, f: dict, sort: str | None, per_column: int) -> CompanyColumn:  # type: ignore[no-untyped-def]
    stmt = _filtered(stmt, **f)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(*_order(sort)).limit(per_column)))
    return CompanyColumn(key=key, label=label, total=total, cards=cards_for(db, rows))


def _stage_stmt(stage: str, sub: str):  # type: ignore[no-untyped-def]
    stmt = select(Company).where(Company.stage == stage, Company.off_ramp.is_(None), Company.sub_status == sub)
    if stage == "prospect":
        stmt = stmt.where(~on_enquiries_board())  # SMA-33: enquiry-path companies are on the Enquiries board
    return stmt


def tab_board(db: Session, tab: str, *, now: datetime | None = None, q: str | None = None, characteristic_id: int | None = None,
              priority_filter: str | None = None, category: str | None = None, flag: str | None = None, sort: str | None = None,
              per_column: int = 50, offering_id: int | None = None) -> CompanyBoard:
    """The board of the Prospects or Leads tab: one column per status, nothing from another board (no Became lead /
    Won / Closed lanes - a company is only ever on one board)."""
    f = dict(q=q, characteristic_id=characteristic_id, priority_filter=priority_filter, category=category, flag=flag,
             offering_id=offering_id)
    stage, subs = ("prospect", PROSPECT_STAGES) if tab == "prospects" else ("lead", COMPANY_SUB_STATUSES["lead"])
    return CompanyBoard(columns=[_column(db, sub, lifecycle.sub_label(sub), _stage_stmt(stage, sub), f, sort, per_column) for sub in subs])


def company_list(db: Session, *, stage: str | None = None, sub_status: str | None = None, closed: bool | None = False,
                 q: str | None = None, characteristic_id: int | None = None, priority_filter: str | None = None,
                 category: str | None = None, flag: str | None = None, sort: str | None = None, page: int = 1,
                 page_size: int | None = None, offering_id: int | None = None) -> CompanyListPage:
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
        if stage == "prospect":
            stmt = stmt.where(~on_enquiries_board())
    stmt = _filtered(stmt, q=q, characteristic_id=characteristic_id, priority_filter=priority_filter, category=category, flag=flag,
                     offering_id=offering_id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(*_order(sort)).limit(size).offset((page - 1) * size)))
    return CompanyListPage(items=cards_for(db, rows), total=total, page=page, page_size=size)


def counts(db: Session, *, offering_id: int | None = None) -> dict[str, int]:
    """Active companies per stage (the filter pills) and the closed total; with ``offering_id`` only that offering's
    (the same rule as the boards: companies tagged with it; enquiries for it or whose company is tagged with it)."""
    from sales0.services.crm import company_offerings

    company_clause = Company.id.in_(company_offerings.companies_with(offering_id)) if offering_id else true()
    enquiry_clause = ((Enquiry.offering_id == offering_id) | Enquiry.company_id.in_(company_offerings.companies_with(offering_id))
                      if offering_id else true())
    out = {s: 0 for s in COMPANY_STAGES}
    for stage, n in db.execute(select(Company.stage, func.count()).where(Company.stage.is_not(None), Company.off_ramp.is_(None),
                                                                         ~on_enquiries_board(), company_clause).group_by(Company.stage)).all():
        out[stage] = int(n)
    out["enquiries"] = int(db.scalar(select(func.count()).select_from(Enquiry).where(
        Enquiry.review_status == "accepted", _enquiry_on_board(), enquiry_clause)) or 0)
    out["closed"] = int(db.scalar(select(func.count()).select_from(Company).where(
        Company.off_ramp.is_not(None), Company.stage.is_not(None), company_clause)) or 0)
    return out


def _enquiry_on_board():  # type: ignore[no-untyped-def]
    """An accepted enquiry is on the Enquiries board while its company is not on another board."""
    return or_(Enquiry.company_id.is_(None), Enquiry.company_id.in_(
        select(Company.id).where(or_(Company.stage.is_(None), on_enquiries_board()))))


def enquiry_board(db: Session, *, q: str | None = None, characteristic_id: int | None = None, priority_filter: str | None = None,
                  category: str | None = None, per_column: int = 50, offering_id: int | None = None) -> EnquiryBoard:
    cols: list[EnquiryColumn] = []
    for status in (*ENQUIRY_ACTIVE_STATUSES, *ENQUIRY_CLOSED_STATUSES):
        shown = priority.of_enquiry()  # SMA-60: the company's priority (as on its page), else the enquiry's own
        stmt = select(Enquiry, Company.name, Contact.full_name, Contact.title, shown).outerjoin(Company, Company.id == Enquiry.company_id).outerjoin(
            Contact, Contact.id == Enquiry.contact_id).where(Enquiry.review_status == "accepted", Enquiry.deal_status == status, _enquiry_on_board())
        if q and q.strip():
            like = f"%{_keys.like_escape(_keys.name_key(q))}%"
            stmt = stmt.where(or_(func.lower(func.coalesce(Company.name, "")).like(like, escape="\\"),
                                  func.lower(func.coalesce(Enquiry.requirement_summary, "")).like(like, escape="\\")))
        clause = priority.filter_clause(shown, priority_filter) if priority_filter else None
        if clause is not None:
            stmt = stmt.where(clause)
        if category in ("channel_partner", "competitor", "relevant_prospect"):
            stmt = stmt.where(Enquiry.category == category)
        if characteristic_id:
            stmt = stmt.where(Enquiry.company_id.in_(companies_with_trait(characteristic_id)))
        if offering_id:  # the enquiry is for that offering, or its company is tagged with it
            from sales0.services.crm import company_offerings

            stmt = stmt.where((Enquiry.offering_id == offering_id) | Enquiry.company_id.in_(company_offerings.companies_with(offering_id)))
        total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = db.execute(stmt.order_by(priority.rank_order(shown), Enquiry.date_added.desc(), Enquiry.id.desc()).limit(per_column)).all()
        company_ids = {r[0].company_id for r in rows if r[0].company_id}
        by_company = {c.id: c for c in cards_for(db, list(db.scalars(select(Company).where(Company.id.in_(company_ids))))) } if company_ids else {}
        cols.append(EnquiryColumn(key=status, label=deals.deal_label(status), total=total, cards=[
            EnquiryCard(id=e.id, summary=e.requirement_summary, company_id=e.company_id, company_name=name or e.company,
                        deal_status=e.deal_status, priority=prio, category=e.category or "relevant_prospect",
                        date_added=e.date_added, source_url=e.source_url, author_profile_url=e.author_profile_url,
                        contact_id=e.contact_id, person_name=cname or e.contact_person or e.author_name,
                        person_title=ctitle or (e.author_headline if not cname and not e.contact_person else None),
                        company_card=by_company.get(e.company_id))
            for e, name, cname, ctitle, prio in rows
        ]))
    return EnquiryBoard(columns=cols)


__all__ = ["CompanyBoard", "CompanyCard", "EnquiryBoard", "cards_for", "company_list", "counts", "enquiry_board", "tab_board"]

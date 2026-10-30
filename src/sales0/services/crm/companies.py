"""Companies: get-or-create (matched by name, website domain or LinkedIn company page), the company roll-up
(prospects, leads, clients, contacts, enquiries, outreach, replies, timeline), the Companies list, edits and merge."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from sales0.db.models import (
    Client,
    Company,
    Competitor,
    Contact,
    ContactLink,
    Enquiry,
    EntityCharacteristic,
    Characteristic,
    Flag,
    FollowUp,
    Lead,
    OutreachMessage,
    OutreachReply,
    Prospect,
    TeamMember,
    TimelineEvent,
    WorkerTask,
)
from sales0.services._common import now_or, not_found
from sales0.services.crm import _keys, timeline
from sales0.services.crm._common import UNSET, invalid, page_params
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.views import (
    CharacteristicView,
    CompanyDetail,
    CompanyPage,
    CompanyRow,
    CompanyView,
    LinkedMessageView,
    LinkedReplyView,
)

_OPEN_FU = (FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None))


def _view(c: Company) -> CompanyView:
    return CompanyView(id=c.id, name=c.name, website=c.website, city=c.city, linkedin_url=c.linkedin_url, notes=c.notes,
                       stage=c.stage, sub_status=c.sub_status, off_ramp=c.off_ramp)


# ---------------------------------------------------------------------------- matching
# One key per fact, shared with the inbox's pipeline check (services.crm.pipeline_match): name -> ``name_key``,
# website / e-mail domain -> ``domain_key``, LinkedIn company page -> ``linkedin_key`` (helpers in ``_keys``).
def by_name(db: Session, name: str | None) -> Company | None:
    key = _keys.name_key(name)
    return db.scalar(select(Company).where(Company.name_key == key[:200])) if key else None


def by_domain(db: Session, website: str | None) -> Company | None:
    """Same website host (``_keys.domain_key``); generic hosts (linkedin.com, gmail.com, ...) never match."""
    dk = _keys.domain_key(website)
    if not dk:
        return None
    return db.scalar(select(Company).where(Company.domain_key == dk).order_by(Company.id).limit(1))


def by_linkedin(db: Session, linkedin_url: str | None) -> Company | None:
    """Same LinkedIn company page (``_keys.company_linkedin_key``: ``company/<slug>``)."""
    lk = _keys.company_linkedin_key(linkedin_url)
    if not lk:
        return None
    return db.scalar(select(Company).where(Company.linkedin_key == lk).order_by(Company.id).limit(1))


def find_match(
    db: Session, name: str | None = None, *, website: str | None = None, linkedin_url: str | None = None
) -> Company | None:
    """The existing company for these facts: same name key, else same website domain, else same LinkedIn company
    page. Generic hosts (linkedin.com, gmail.com, ...) never match (see ``_keys.domain_key``)."""
    return by_name(db, name) or by_domain(db, website) or by_linkedin(db, linkedin_url)


def _set_website(row: Company, website: str | None) -> None:
    web = _keys.collapse(website)[:300] or None
    row.website, row.website_key, row.domain_key = web, _keys.website_key(web), _keys.domain_key(web)


def _set_linkedin(row: Company, url: str | None) -> None:
    text = _keys.collapse(url)[:500] or None
    row.linkedin_url, row.linkedin_key = text, _keys.company_linkedin_key(text)


def get_or_create_row(
    db: Session, name: str | None, *, website: str | None = None, city: str | None = None, linkedin_url: str | None = None,
    now: datetime | None = None, actor: Actor = SYSTEM,
) -> Company | None:
    """The ORM row for ``name`` (matched by name, website domain or LinkedIn page; created if new); None for a blank
    name. Fills an empty website / city / LinkedIn URL of an existing row, never overwrites one."""
    clean = _keys.collapse(name)[:200]
    if not clean:
        return None
    row = find_match(db, clean, website=website, linkedin_url=linkedin_url)
    web = _keys.collapse(website)[:300] or None
    town = _keys.collapse(city)[:100] or None
    lk_url = linkedin_url if _keys.company_linkedin_key(linkedin_url) else None
    if row is None:
        row = Company(name=clean, name_key=_keys.name_key(clean), city=town, created_at=now_or(now), updated_at=now_or(now))
        _set_website(row, web)
        _set_linkedin(row, lk_url)
        db.add(row)
        db.flush()
        timeline.add_event(db, subject_type="company", subject_id=row.id, kind="created", body=f"Company added: {clean}.",
                           data={"name": clean}, actor=actor, occurred_at=now_or(now))
        return row
    changed = False
    if web and not row.website:
        _set_website(row, web)
        changed = True
    if lk_url and not row.linkedin_url:
        _set_linkedin(row, lk_url)
        changed = True
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


def get_or_none(db: Session, company_id: int | None) -> CompanyView | None:
    row = db.get(Company, company_id) if company_id else None
    return _view(row) if row is not None else None


def _get(db: Session, company_id: int) -> Company:
    c = db.get(Company, company_id)
    if c is None:
        raise not_found()
    return c


# ---------------------------------------------------------------------------- roll-up
def _characteristics_by_company(db: Session, company_ids: list[int]) -> dict[int, list[CharacteristicView]]:
    """Union of the characteristics of a company's prospects and of its prospect-less clients."""
    out: dict[int, dict[int, Characteristic]] = defaultdict(dict)
    if not company_ids:
        return {}
    via_prospect = (
        select(Prospect.company_id, Characteristic)
        .join(EntityCharacteristic, (EntityCharacteristic.entity_type == "prospect") & (EntityCharacteristic.entity_id == Prospect.id))
        .join(Characteristic, Characteristic.id == EntityCharacteristic.characteristic_id)
        .where(Prospect.company_id.in_(company_ids))
    )
    via_client = (
        select(Client.company_id, Characteristic)
        .join(EntityCharacteristic, (EntityCharacteristic.entity_type == "client") & (EntityCharacteristic.entity_id == Client.id))
        .join(Characteristic, Characteristic.id == EntityCharacteristic.characteristic_id)
        .where(Client.company_id.in_(company_ids))
    )
    for stmt in (via_prospect, via_client):
        for cid, ch in db.execute(stmt).all():
            out[cid][ch.id] = ch
    return {
        cid: [CharacteristicView(id=c.id, name=c.name, system_key=c.system_key, active=c.active)
              for c in sorted(chs.values(), key=lambda c: (c.sort_order, c.name_key))]
        for cid, chs in out.items()
    }


def _open_follow_ups(db: Session, company_ids: list[int]) -> dict[int, int]:
    if not company_ids:
        return {}
    by_p = db.execute(
        select(Prospect.company_id, func.count(FollowUp.id)).join(FollowUp, FollowUp.prospect_id == Prospect.id)
        .where(Prospect.company_id.in_(company_ids), *_OPEN_FU).group_by(Prospect.company_id)
    ).all()
    by_l = db.execute(
        select(Prospect.company_id, func.count(FollowUp.id)).join(Lead, Lead.prospect_id == Prospect.id)
        .join(FollowUp, FollowUp.lead_id == Lead.id)
        .where(Prospect.company_id.in_(company_ids), *_OPEN_FU).group_by(Prospect.company_id)
    ).all()
    out: dict[int, int] = defaultdict(int)
    for cid, n in [*by_p, *by_l]:
        out[cid] += int(n)
    return out


def _counts(db: Session, model, company_ids: list[int]) -> dict[int, int]:  # type: ignore[no-untyped-def]
    if not company_ids:
        return {}
    return {cid: int(n) for cid, n in db.execute(
        select(model.company_id, func.count()).where(model.company_id.in_(company_ids)).group_by(model.company_id)
    ).all()}


def get_company(db: Session, company_id: int) -> CompanyDetail:
    from sales0.services.crm import clients, contacts, leads, prospects

    c = _get(db, company_id)
    prospect_rows = list(db.scalars(select(Prospect).where(Prospect.company_id == c.id).order_by(Prospect.updated_at.desc(), Prospect.id.desc())))
    pids = [p.id for p in prospect_rows]
    enquiry_rows = list(db.scalars(
        select(Enquiry).where(or_(Enquiry.company_id == c.id, Enquiry.prospect_id.in_(pids or [-1])))
        .order_by(Enquiry.date_added.desc(), Enquiry.id.desc())
    ))
    eids = [e.id for e in enquiry_rows]
    # every person of the company: its contacts plus the contacts linked to its prospects
    linked = select(ContactLink.contact_id).where(ContactLink.entity_type == "prospect", ContactLink.entity_id.in_(pids or [-1]))
    primary = select(Prospect.contact_id).where(Prospect.id.in_(pids or [-1]), Prospect.contact_id.is_not(None))
    contact_rows = list(db.scalars(
        select(Contact).where(or_(Contact.company_id == c.id, Contact.id.in_(linked), Contact.id.in_(primary))).order_by(Contact.id).limit(300)
    ))
    lead_rows = list(db.scalars(select(Lead).where(or_(Lead.company_id == c.id, Lead.prospect_id.in_(pids or [-1]))).order_by(Lead.updated_at.desc(), Lead.id.desc())))
    client_rows = list(db.scalars(select(Client).where(or_(Client.company_id == c.id, Client.prospect_id.in_(pids or [-1]))).order_by(Client.created_at.desc(), Client.id.desc())))
    conds = [OutreachMessage.company_id == c.id]
    if pids:
        conds.append(OutreachMessage.prospect_id.in_(pids))
    if eids:
        conds.append(OutreachMessage.enquiry_id.in_(eids))
    msg_rows = list(db.execute(
        select(OutreachMessage, TeamMember.name).join(TeamMember, TeamMember.id == OutreachMessage.member_id)
        .where(or_(*conds)).order_by(OutreachMessage.created_at.desc(), OutreachMessage.id.desc()).limit(200)
    ).all())
    msg_ids = [m.id for m, _ in msg_rows]
    reply_rows = list(db.scalars(select(OutreachReply).where(OutreachReply.message_id.in_(msg_ids)).order_by(OutreachReply.received_at.desc()))) if msg_ids else []
    return CompanyDetail(
        **_view(c).model_dump(),
        prospects=prospects.views_for(db, prospect_rows),
        enquiries=[prospects.linked_enquiry_view(e) for e in enquiry_rows],
        contacts=contacts.views_for(db, contact_rows),
        timeline=timeline.list_timeline(db, company_id=c.id),
        leads=leads.views_for(db, lead_rows),
        clients=clients.views_for(db, client_rows),
        messages=[LinkedMessageView(id=m.id, subject=m.subject, state=m.state, sent_at=m.sent_at, reply_status=m.reply_status, owner_name=o)
                  for m, o in msg_rows],
        replies=[LinkedReplyView(id=r.id, message_id=r.message_id, received_at=r.received_at, kind=r.kind, intent=r.intent, snippet=r.snippet or None)
                 for r in reply_rows],
        characteristics=_characteristics_by_company(db, [c.id]).get(c.id, []),
        open_follow_ups=_open_follow_ups(db, [c.id]).get(c.id, 0),
    )


def list_companies(
    db: Session, *, q: str | None = None, characteristic_id: int | None = None, page: int = 1, page_size: int | None = None,
) -> CompanyPage:
    page, size = page_params(page, page_size)
    stmt = select(Company)
    if q and q.strip():
        like = f"%{_keys.like_escape(_keys.name_key(q))}%"
        stmt = stmt.where(or_(Company.name_key.like(like, escape="\\"), func.lower(func.coalesce(Company.website, "")).like(like, escape="\\")))
    if characteristic_id:
        from sales0.services.crm import characteristics

        stmt = stmt.where(or_(
            Company.id.in_(select(Prospect.company_id).where(characteristics.prospect_filter(characteristic_id))),
            Company.id.in_(select(Client.company_id).where(characteristics.client_filter(characteristic_id))),
        ))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(Company.name_key, Company.id).limit(size).offset((page - 1) * size)))
    ids = [r.id for r in rows]
    np_, ncl, nct, nen = (_counts(db, m, ids) for m in (Prospect, Client, Contact, Enquiry))
    fus, traits = _open_follow_ups(db, ids), _characteristics_by_company(db, ids)
    best: dict[int, str] = {}  # q16: the highest priority among the company's open prospects
    if ids:
        from sales0.services.crm.priority import RANK

        for cid, prio in db.execute(
            select(Prospect.company_id, Prospect.priority).where(Prospect.company_id.in_(ids), Prospect.status == "open", Prospect.priority.is_not(None))
        ).all():
            if prio in RANK and RANK[prio] > RANK.get(best.get(cid, ""), 0):
                best[cid] = prio
    items = [
        CompanyRow(**_view(r).model_dump(), prospects=np_.get(r.id, 0), clients=ncl.get(r.id, 0), contacts=nct.get(r.id, 0),
                   enquiries=nen.get(r.id, 0), open_follow_ups=fus.get(r.id, 0), characteristics=traits.get(r.id, []),
                   priority=best.get(r.id))
        for r in rows
    ]
    return CompanyPage(items=items, total=total, page=page, page_size=size)


# ---------------------------------------------------------------------------- edit / merge
def update_company(
    db: Session, company_id: int, *, name=UNSET, website=UNSET, linkedin_url=UNSET, city=UNSET, notes=UNSET,  # type: ignore[no-untyped-def]
    actor: Actor = TEAM, now: datetime | None = None,
) -> CompanyView:
    row = _get(db, company_id)
    changed: list[str] = []
    if name is not UNSET:
        clean = _keys.collapse(name)[:200]
        if not clean:
            raise invalid("company_invalid", "A company needs a name.")
        other = db.scalar(select(Company.id).where(Company.name_key == _keys.name_key(clean)))
        if other is not None and other != row.id:
            raise invalid("company_invalid", "Another company already has that name - merge the two instead.")
        if clean != row.name:
            row.name, row.name_key = clean, _keys.name_key(clean)
            changed.append("name")
    if website is not UNSET:
        web = _keys.collapse(website)[:300] or None
        if web and not _keys.website_key(web):
            raise invalid("company_invalid", "That website does not look like a web address (e.g. acme.com).")
        if web != row.website:
            _set_website(row, web)
            changed.append("website")
    if linkedin_url is not UNSET:
        text = _keys.collapse(linkedin_url)[:500] or None
        if text and not _keys.company_linkedin_key(text):
            raise invalid("company_invalid", "Use the LinkedIn company page link (linkedin.com/company/...).")
        if text != row.linkedin_url:
            _set_linkedin(row, text)
            changed.append("linkedin_url")
    if city is not UNSET:
        town = _keys.collapse(city)[:100] or None
        if town != row.city:
            row.city = town
            changed.append("city")
    if notes is not UNSET and ((notes or "").strip() or None) != row.notes:
        row.notes = (notes or "").strip() or None
        changed.append("notes")
    if changed:
        row.updated_at = now_or(now)
        db.flush()
        if "name" in changed:
            from sales0.services.crm import contacts

            for contact in db.scalars(select(Contact).where(Contact.company_id == row.id)):
                contacts._rebuild_search_text(db, contact)  # noqa: SLF001 - the company name is part of the search text
        timeline.add_event(db, subject_type="company", subject_id=row.id, kind="note", body="Details edited: " + ", ".join(changed),
                           data={"system": True, "fields": changed}, actor=actor, occurred_at=now)
    return _view(row)


def merge(db: Session, keep_id: int, drop_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> CompanyView:
    """Fold the duplicate ``drop_id`` into ``keep_id``: every record pointing at it moves, empty fields are filled,
    the duplicate is deleted and both timelines keep a trace."""
    if keep_id == drop_id:
        raise invalid("merge_self", "Choose two different companies to merge.")
    keep, drop = _get(db, keep_id), _get(db, drop_id)
    stamp = now_or(now)
    for model in (Prospect, Lead, Client, Contact, Enquiry, OutreachMessage, Competitor, TimelineEvent, WorkerTask):
        db.execute(update(model).where(model.company_id == drop.id).values(company_id=keep.id))
    have = {(ln.contact_id) for ln in db.scalars(select(ContactLink).where(ContactLink.entity_type == "company", ContactLink.entity_id == keep.id))}
    for ln in list(db.scalars(select(ContactLink).where(ContactLink.entity_type == "company", ContactLink.entity_id == drop.id))):
        if ln.contact_id in have:
            db.delete(ln)
        else:
            ln.entity_id = keep.id
    active_kinds = set(db.scalars(select(Flag.kind).where(Flag.entity_type == "company", Flag.entity_id == keep.id, Flag.cleared_at.is_(None))))
    for fl in list(db.scalars(select(Flag).where(Flag.entity_type == "company", Flag.entity_id == drop.id))):
        if fl.cleared_at is None and fl.kind in active_kinds:
            db.delete(fl)
        else:
            fl.entity_id = keep.id
    db.execute(update(TimelineEvent).where(TimelineEvent.subject_type == "company", TimelineEvent.subject_id == drop.id).values(subject_id=keep.id))
    if not keep.website and drop.website:
        _set_website(keep, drop.website)
    if not keep.linkedin_url and drop.linkedin_url:
        _set_linkedin(keep, drop.linkedin_url)
    if not keep.city and drop.city:
        keep.city = drop.city
    if drop.notes:
        keep.notes = (keep.notes + "\n\n" + drop.notes) if keep.notes else drop.notes
    dropped_name = drop.name
    keep.updated_at = stamp
    db.flush()
    db.delete(drop)
    db.flush()
    from sales0.services.crm import contacts

    for contact in db.scalars(select(Contact).where(Contact.company_id == keep.id)):
        contacts._rebuild_search_text(db, contact)  # noqa: SLF001 - the company name is part of the search text
    timeline.add_event(db, subject_type="company", subject_id=keep.id, kind="note",
                       body=f"Merged duplicate company {dropped_name} into this one.",
                       data={"system": True, "merged_company_id": drop_id, "merged_company_name": dropped_name}, actor=actor, occurred_at=stamp)
    return _view(keep)


def duplicate_candidates(db: Session, company_id: int, *, limit: int = 10) -> list[CompanyView]:
    """Other companies that look like the same one (same domain / LinkedIn page, or a name that starts the same)."""
    c = _get(db, company_id)
    conds = []
    if c.domain_key:
        conds.append(Company.domain_key == c.domain_key)
    if c.linkedin_key:
        conds.append(Company.linkedin_key == c.linkedin_key)
    first = (c.name_key.split(" ") or [""])[0]
    if len(first) >= 3:
        conds.append(Company.name_key.like(f"{_keys.like_escape(first)}%", escape="\\"))
    if not conds:
        return []
    rows = db.scalars(select(Company).where(Company.id != c.id, or_(*conds)).order_by(Company.name_key).limit(limit))
    return [_view(r) for r in rows]


__all__ = [
    "duplicate_candidates", "find_match", "get_company", "get_or_none", "get_or_create", "get_or_create_row", "list_companies", "merge",
    "update_company",
]


PLACEHOLDER_PREFIXES = ("Company TBD #", "Unnamed")


def is_placeholder(name: str | None) -> bool:
    return bool(name) and name.startswith(PLACEHOLDER_PREFIXES)


def attach_to_enquiry(
    db: Session, enquiry_id: int, *, name: str | None = None, company_id: int | None = None,
    actor: Actor = TEAM, now: datetime | None = None,
) -> CompanyView:
    """Attach a company to an enquiry: an existing one (``company_id``, or ``name`` matching one) or a new one
    (``name``). The enquiry's own prospect follows it. A placeholder company left empty by the move is folded
    into the real one, so nothing is left behind."""
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    stamp = now_or(now)
    if company_id is not None:
        target = _get(db, company_id)
    else:
        target = get_or_create_row(db, name, now=stamp, actor=actor)
        if target is None:
            raise invalid("company_invalid", "Type the company's name (or pick one from the list).")
    old_id = e.company_id
    if old_id == target.id:
        e.company = target.name
        db.flush()
        return _view(target)
    old = db.get(Company, old_id) if old_id else None
    e.company, e.company_id = target.name, target.id
    p = db.get(Prospect, e.prospect_id) if e.prospect_id else None
    if p is not None:
        if is_placeholder(p.name) or (old is not None and p.name == old.name):
            p.name = target.name[:200]
        p.company_id = target.id
    db.flush()
    timeline.add_event(
        db, subject_type="enquiry", subject_id=e.id, kind="note", actor=actor, occurred_at=stamp,
        body=f"Company set to {target.name}" + (f" (was {old.name})." if old is not None else "."),
        data={"system": True, "company_id": target.id, "previous_company_id": old_id},
    )
    if old is not None and is_placeholder(old.name):
        still_used = db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.company_id == old.id))
        if not still_used:
            merge(db, target.id, old.id, actor=actor, now=stamp)
    return _view(target)

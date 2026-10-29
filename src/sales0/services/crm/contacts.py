"""Shared people: upsert with matching, search, tags, links, duplicates and merge."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from sales0.db.models import (
    Company,
    Contact,
    ContactEmail,
    ContactLink,
    ContactPhone,
    ContactTag,
    Enquiry,
    Prospect,
    Tag,
    TimelineEvent,
)
from sales0.db.models.enums_crm import LINK_ENTITY_TYPES, TAG_CATEGORIES
from sales0.errors import DomainError
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import _keys, companies, timeline
from sales0.services.crm._common import UNSET, invalid, page_params
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.views import (
    ContactDetail,
    ContactEmailView,
    ContactLinkView,
    ContactPhoneView,
    ContactPage,
    ContactView,
    DuplicateGroup,
    TagView,
)

_AUTO_NOTE = {"system": True}


# ---------------------------------------------------------------------------- views
def views_for(db: Session, rows: Iterable[Contact]) -> list[ContactView]:
    """Build views for many contacts with two extra queries (company names, tags)."""
    rows = list(rows)
    if not rows:
        return []
    ids = [r.id for r in rows]
    company_ids = {r.company_id for r in rows if r.company_id}
    names = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids))).all()) if company_ids else {}
    tags: dict[int, list[TagView]] = defaultdict(list)
    for cid, t in db.execute(
        select(ContactTag.contact_id, Tag).join(Tag, Tag.id == ContactTag.tag_id).where(ContactTag.contact_id.in_(ids)).order_by(Tag.name_key)
    ).all():
        tags[cid].append(TagView(id=t.id, name=t.name, category=t.category, count=0))
    return [
        ContactView(
            id=r.id, full_name=r.full_name, email=r.email, phone=r.phone, linkedin_url=r.linkedin_url, title=r.title,
            company_id=r.company_id, company_name=names.get(r.company_id), tags=tags.get(r.id, []), origin=r.origin,
        )
        for r in rows
    ]


def _view(db: Session, row: Contact) -> ContactView:
    return views_for(db, [row])[0]


def _merge_events(db: Session) -> list[TimelineEvent]:
    return list(db.scalars(select(TimelineEvent).where(TimelineEvent.subject_type == "contact", TimelineEvent.kind == "contact_merged").order_by(TimelineEvent.id)))


def resolve_id(db: Session, contact_id: int) -> int | None:
    """``contact_id`` itself, or - when that contact was merged away - the contact it was merged into (None if neither)."""
    seen: set[int] = set()
    current = contact_id
    while db.get(Contact, current) is None:
        if current in seen:
            return None
        seen.add(current)
        nxt = next((ev.subject_id for ev in _merge_events(db) if (ev.data or {}).get("dropped_id") == current), None)
        if nxt is None:
            return None
        current = nxt
    return current


def _merged_into_slug(db: Session, slug: str) -> Contact | None:
    """The surviving contact of a merge that absorbed a contact whose LinkedIn slug was ``slug``."""
    for ev in reversed(_merge_events(db)):
        if slug in ((ev.data or {}).get("dropped_slugs") or []):
            row = db.get(Contact, ev.subject_id)
            if row is not None:
                return row
    return None


def _get(db: Session, contact_id: int) -> Contact:
    row = db.get(Contact, contact_id)
    if row is None:
        target = resolve_id(db, contact_id)
        row = db.get(Contact, target) if target is not None else None
    if row is None:
        raise not_found()
    return row


def _rebuild_search_text(db: Session, row: Contact) -> None:
    emails = list(db.scalars(select(ContactEmail.email).where(ContactEmail.contact_id == row.id).order_by(ContactEmail.id)))
    if row.email and row.email not in emails:
        emails.insert(0, row.email)
    company = db.scalar(select(Company.name).where(Company.id == row.company_id)) if row.company_id else None
    tag_names = list(db.scalars(select(Tag.name_key).join(ContactTag, ContactTag.tag_id == Tag.id).where(ContactTag.contact_id == row.id)))
    row.search_text = _keys.search_text(row.full_name, emails, company, tag_names)
    db.flush()


# ---------------------------------------------------------------------------- upsert
def _find_match(db: Session, *, emails: list[str], slug: str | None, digits: str, name: str | None) -> Contact | None:
    if emails:
        cid = db.scalar(select(ContactEmail.contact_id).where(ContactEmail.email.in_(emails)).order_by(ContactEmail.id).limit(1))
        if cid is not None:
            return db.get(Contact, cid)
    if slug:
        row = db.scalar(select(Contact).where(Contact.linkedin_slug == slug))
        if row is not None:
            return row
        row = _merged_into_slug(db, slug)
        if row is not None:
            return row
    if digits and len(digits) >= 7 and name:
        cids = list(db.scalars(select(ContactPhone.contact_id).where(ContactPhone.digits == digits).limit(20)))
        for cid in cids:
            cand = db.get(Contact, cid)
            if cand is not None and _keys.names_match_loosely(cand.full_name, name):
                return cand
    return None


def _add_email(db: Session, row: Contact, email: str, *, primary: bool) -> bool:
    if db.scalar(select(ContactEmail.id).where(ContactEmail.email == email)) is not None:
        return False
    db.add(ContactEmail(contact_id=row.id, email=email, is_primary=primary))
    db.flush()
    return True


def _add_phone(db: Session, row: Contact, phone: str) -> bool:
    digits = _keys.phone_digits(phone)
    if len(digits) < 6:
        return False
    if db.scalar(select(ContactPhone.id).where(ContactPhone.contact_id == row.id, ContactPhone.digits == digits)) is None:
        db.add(ContactPhone(contact_id=row.id, phone=phone[:40], digits=digits))
        db.flush()
        return True
    return False


def upsert_row(
    db: Session,
    *,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    linkedin_url: str | None = None,
    title: str | None = None,
    company_id: int | None = None,
    origin: str = "manual",
    actor: Actor = SYSTEM,
    now: datetime | None = None,
) -> tuple[Contact, bool]:
    """Find-or-create; returns (row, created). Fills empty columns, never blanks one."""
    stamp = now_or(now)
    name = clip(_keys.collapse(full_name), 200)
    mail = _keys.first_email(email)
    slug = _keys.linkedin_slug_of(linkedin_url)
    tel = _keys.display_phone(phone)
    if not name and not mail and not slug:
        raise invalid("contact_invalid", "A contact needs a name, an email or a LinkedIn URL.")
    emails = _keys.all_emails(email)
    row = _find_match(db, emails=emails, slug=slug, digits=_keys.phone_digits(tel), name=name)
    created = row is None
    enriched: list[str] = []
    if row is None:
        row = Contact(
            full_name=name, email=mail, phone=tel, linkedin_slug=slug, title=clip(title, 200),
            linkedin_url=clip(linkedin_url, 500) if slug else None, company_id=company_id,
            origin=origin if origin in ("manual", "enquiry", "sheet", "import", "reply") else "manual",
            actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
        )
        db.add(row)
        db.flush()
    else:
        enriched: list[str] = []
        if name and not row.full_name:
            row.full_name = name
            enriched.append("name")
        if mail and not row.email:
            row.email = mail
            enriched.append("email")
        if tel and not row.phone:
            row.phone = tel
            enriched.append("phone")
        if slug and not row.linkedin_slug and db.scalar(select(Contact.id).where(Contact.linkedin_slug == slug)) is None:
            row.linkedin_slug = slug
            row.linkedin_url = clip(linkedin_url, 500)
            enriched.append("linkedin")
        if title and not row.title:
            row.title = clip(title, 200)
            enriched.append("title")
        if company_id and not row.company_id:
            row.company_id = company_id
            enriched.append("company")
        row.updated_at = stamp
    for i, addr in enumerate(emails):
        if _add_email(db, row, addr, primary=(row.email == addr)) and not created:
            enriched.append("extra email")
    if tel and _add_phone(db, row, tel) and not created and "phone" not in enriched:
        enriched.append("extra phone")
    _rebuild_search_text(db, row)
    if created:
        timeline.add_event(
            db, subject_type="contact", subject_id=row.id, kind="created",
            body=f"Contact added ({origin}).", data={"origin": origin}, actor=actor, occurred_at=stamp,
        )
    elif enriched:
        timeline.add_event(
            db, subject_type="contact", subject_id=row.id, kind="note", body="Details added: " + ", ".join(enriched),
            data={**_AUTO_NOTE, "fields": enriched, "origin": origin}, actor=actor, occurred_at=stamp,
        )
    return row, created


def upsert(
    db: Session,
    *,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    linkedin_url: str | None = None,
    title: str | None = None,
    company_id: int | None = None,
    origin: str = "manual",
    actor: Actor = SYSTEM,
    now: datetime | None = None,
) -> ContactView:
    row, _ = upsert_row(
        db, full_name=full_name, email=email, phone=phone, linkedin_url=linkedin_url, title=title,
        company_id=company_id, origin=origin, actor=actor, now=now,
    )
    return _view(db, row)


# ---------------------------------------------------------------------------- read
def _email_rows(db: Session, contact_id: int) -> list[ContactEmail]:
    return list(db.scalars(select(ContactEmail).where(ContactEmail.contact_id == contact_id).order_by(ContactEmail.is_primary.desc(), ContactEmail.id)))


def _phone_rows(db: Session, row: Contact) -> list[ContactPhone]:
    rows = list(db.scalars(select(ContactPhone).where(ContactPhone.contact_id == row.id).order_by(ContactPhone.id)))
    primary = _keys.phone_digits(row.phone)
    return sorted(rows, key=lambda r: (r.digits != primary, r.id))


def get_contact(db: Session, contact_id: int) -> ContactDetail:
    from sales0.db.models import LinkedinProfile
    from sales0.services import suppressions

    row = _get(db, contact_id)
    base = _view(db, row)
    email_rows = _email_rows(db, row.id)
    blocked = set(suppressions.suppressed_among(db, [e.email for e in email_rows])) if email_rows else set()
    phone_rows = _phone_rows(db, row)
    primary_digits = _keys.phone_digits(row.phone)
    prof = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == row.linkedin_slug)) if row.linkedin_slug else None
    return ContactDetail(
        **base.model_dump(), emails=[e.email for e in email_rows], phones=[p.phone for p in phone_rows], notes=row.notes,
        email_items=[ContactEmailView(id=e.id, email=e.email, is_primary=e.email == row.email, suppressed=e.email in blocked) for e in email_rows],
        phone_items=[ContactPhoneView(id=p.id, phone=p.phone, is_primary=bool(primary_digits) and p.digits == primary_digits) for p in phone_rows],
        linkedin_profile_id=prof.id if prof else None, tracked=prof.tracked if prof else None,
        links=_link_views(db, row.id), timeline=timeline.list_timeline(db, contact_id=row.id),
    )


def _link_views(db: Session, contact_id: int) -> list[ContactLinkView]:
    from sales0.db.models import Client, Competitor, Lead

    out: list[ContactLinkView] = []
    for ln in db.scalars(select(ContactLink).where(ContactLink.contact_id == contact_id).order_by(ContactLink.id)):
        label, href = f"{ln.entity_type} #{ln.entity_id}", timeline.href_for(ln.entity_type, ln.entity_id)
        if ln.entity_type == "prospect":
            p = db.get(Prospect, ln.entity_id)
            label = p.name if p else label
        elif ln.entity_type == "enquiry":
            e = db.get(Enquiry, ln.entity_id)
            label = (e.requirement_summary or e.company or label) if e else label
            href = f"/pipeline/prospects/{e.prospect_id}" if e is not None and e.prospect_id else None
        elif ln.entity_type == "company":
            c = db.get(Company, ln.entity_id)
            label = c.name if c else label
        elif ln.entity_type == "client":
            c2 = db.get(Client, ln.entity_id)
            label = c2.name if c2 else label
        elif ln.entity_type == "lead":
            ld = db.get(Lead, ln.entity_id)
            p2 = db.get(Prospect, ld.prospect_id) if ld else None
            label = f"Lead: {p2.name}" if p2 else label
        elif ln.entity_type == "competitor":
            cp = db.get(Competitor, ln.entity_id)
            label = cp.name if cp else label
        out.append(ContactLinkView(entity_type=ln.entity_type, entity_id=ln.entity_id, label=label, href=href, role=ln.role))
    return out


def list_contacts(
    db: Session, *, q: str | None = None, tag: str | None = None, company_id: int | None = None,
    page: int = 1, page_size: int | None = None,
) -> ContactPage:
    page, size = page_params(page, page_size)
    stmt = select(Contact)
    if q and q.strip():
        for token in _keys.name_key(q).split(" "):
            stmt = stmt.where(Contact.search_text.like(f"%{_keys.like_escape(token)}%", escape="\\"))
    if tag and tag.strip():
        stmt = stmt.where(
            Contact.id.in_(
                select(ContactTag.contact_id).join(Tag, Tag.id == ContactTag.tag_id).where(Tag.name_key == _keys.name_key(tag))
            )
        )
    if company_id:
        stmt = stmt.where(Contact.company_id == company_id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    order = func.lower(func.coalesce(Contact.full_name, Contact.email, ""))
    rows = list(db.scalars(stmt.order_by(order, Contact.id).limit(size).offset((page - 1) * size)))
    return ContactPage(items=views_for(db, rows), total=total, page=page, page_size=size)


# ---------------------------------------------------------------------------- edit
def update_contact(
    db: Session, contact_id: int, *, full_name=UNSET, title=UNSET, notes=UNSET, company=UNSET,  # type: ignore[no-untyped-def]
    linkedin_url=UNSET, actor: Actor = TEAM, now: datetime | None = None,  # type: ignore[no-untyped-def]
) -> ContactView:
    row = _get(db, contact_id)
    changed: list[str] = []
    if linkedin_url is not UNSET:
        text = (linkedin_url or "").strip()
        slug = _keys.linkedin_slug_of(text) if text else None
        if text and (not slug or not slug.startswith("in/")):
            raise invalid("contact_invalid", "Use the person's LinkedIn profile link (linkedin.com/in/...).")
        if slug != row.linkedin_slug:
            if slug:
                other = db.scalar(select(Contact.id).where(Contact.linkedin_slug == slug, Contact.id != row.id))
                if other is not None:
                    raise DomainError("contact_conflict", f"That LinkedIn profile already belongs to contact #{other} - merge the two instead.", 409)
            if not slug and not row.full_name and not row.email:
                raise invalid("contact_invalid", "A contact needs a name, an email or a LinkedIn URL.")
            row.linkedin_slug = slug
            row.linkedin_url = f"https://www.linkedin.com/{slug}/" if slug else None
            changed.append("linkedin")
    if full_name is not UNSET:
        new = clip(_keys.collapse(full_name), 200)
        if not new and not row.email and not row.linkedin_slug:
            raise invalid("contact_invalid", "A contact needs a name, an email or a LinkedIn URL.")
        if new != row.full_name:
            row.full_name, _ = new, changed.append("name")
    if title is not UNSET and clip(title, 200) != row.title:
        row.title, _ = clip(title, 200), changed.append("title")
    if notes is not UNSET and ((notes or "").strip() or None) != row.notes:
        row.notes, _ = (notes or "").strip() or None, changed.append("notes")
    if company is not UNSET:
        comp = companies.get_or_create_row(db, company, now=now) if company else None
        new_id = comp.id if comp else None
        if new_id != row.company_id:
            row.company_id, _ = new_id, changed.append("company")
    if changed:
        row.updated_at = now_or(now)
        _rebuild_search_text(db, row)
        timeline.add_event(db, subject_type="contact", subject_id=row.id, kind="note",
                           body="Details edited: " + ", ".join(changed), data={**_AUTO_NOTE, "fields": changed},
                           actor=actor, occurred_at=now)
    return _view(db, row)


# ---------------------------------------------------------------------------- e-mails & phones
def _note_change(db: Session, row: Contact, body: str, change: str, actor: Actor, now: datetime | None) -> None:
    row.updated_at = now_or(now)
    _rebuild_search_text(db, row)
    # the value itself stays out of the event body (personal data), like the sheet events
    timeline.add_event(db, subject_type="contact", subject_id=row.id, kind="note", body=body,
                       data={**_AUTO_NOTE, "change": change}, actor=actor, occurred_at=now)


def _sync_primary_email(db: Session, row: Contact) -> None:
    """Keep ``contacts.email`` (the display / primary address) equal to the single ``is_primary`` row."""
    rows = _email_rows(db, row.id)
    primary = next((e for e in rows if e.is_primary), None)
    if primary is None and rows:
        primary = rows[0]
    for e in rows:
        e.is_primary = e is primary
    row.email = primary.email if primary else None
    db.flush()


def _email_of(db: Session, row: Contact, email_id: int) -> ContactEmail:
    e = db.get(ContactEmail, email_id)
    if e is None or e.contact_id != row.id:
        raise not_found()
    return e


def _clean_email(raw: str | None) -> str:
    text = (raw or "").strip().strip("<>").strip()
    mail = _keys.first_email(text)
    if not mail or mail != text.lower() or len(mail) > 254:
        raise invalid("email_invalid", "That is not a valid e-mail address.")
    return mail


def _check_email_free(db: Session, row: Contact, mail: str, own_id: int | None = None) -> None:
    other = db.scalar(select(ContactEmail).where(ContactEmail.email == mail))
    if other is not None and other.id != own_id:
        if other.contact_id == row.id:
            raise invalid("email_invalid", "This contact already has that address.")
        raise DomainError("contact_conflict", f"That address belongs to contact #{other.contact_id} - merge the two contacts instead.", 409)


def add_email(db: Session, contact_id: int, email: str, *, primary: bool = False, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    mail = _clean_email(email)
    _check_email_free(db, row, mail)
    if primary:
        for e in _email_rows(db, row.id):
            e.is_primary = False
    db.add(ContactEmail(contact_id=row.id, email=mail, is_primary=primary or row.email is None))
    db.flush()
    _sync_primary_email(db, row)
    _note_change(db, row, "E-mail added" + (" (primary)" if row.email == mail else ""), "email_added", actor, now)
    return _view(db, row)


def update_email(db: Session, contact_id: int, email_id: int, email: str, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    e = _email_of(db, row, email_id)
    mail = _clean_email(email)
    if mail == e.email:
        return _view(db, row)
    _check_email_free(db, row, mail, e.id)
    e.email = mail
    db.flush()
    _sync_primary_email(db, row)
    _note_change(db, row, "E-mail changed", "email_changed", actor, now)
    return _view(db, row)


def remove_email(db: Session, contact_id: int, email_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    e = _email_of(db, row, email_id)
    if len(_email_rows(db, row.id)) == 1 and not row.full_name and not row.linkedin_slug:
        raise invalid("contact_invalid", "A contact needs a name, an email or a LinkedIn URL - add a name first.")
    db.delete(e)
    db.flush()
    _sync_primary_email(db, row)
    _note_change(db, row, "E-mail removed", "email_removed", actor, now)
    return _view(db, row)


def set_primary_email(db: Session, contact_id: int, email_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    target = _email_of(db, row, email_id)
    if row.email == target.email:
        return _view(db, row)
    for e in _email_rows(db, row.id):
        e.is_primary = e.id == target.id
    db.flush()
    _sync_primary_email(db, row)
    _note_change(db, row, "Primary e-mail changed", "email_primary", actor, now)
    return _view(db, row)


def _phone_of(db: Session, row: Contact, phone_id: int) -> ContactPhone:
    p = db.get(ContactPhone, phone_id)
    if p is None or p.contact_id != row.id:
        raise not_found()
    return p


def _clean_phone(raw: str | None) -> tuple[str, str]:
    tel = _keys.display_phone(raw)
    digits = _keys.phone_digits(tel)
    if not tel or len(digits) < 6:
        raise invalid("phone_invalid", "That is not a valid phone number.")
    return tel[:40], digits


def add_phone(db: Session, contact_id: int, phone: str, *, primary: bool = False, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    tel, digits = _clean_phone(phone)
    if db.scalar(select(ContactPhone.id).where(ContactPhone.contact_id == row.id, ContactPhone.digits == digits)) is not None:
        raise invalid("phone_invalid", "This contact already has that number.")
    db.add(ContactPhone(contact_id=row.id, phone=tel, digits=digits))
    if primary or not row.phone:
        row.phone = tel
    db.flush()
    _note_change(db, row, "Phone added" + (" (primary)" if row.phone == tel else ""), "phone_added", actor, now)
    return _view(db, row)


def update_phone(db: Session, contact_id: int, phone_id: int, phone: str, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    p = _phone_of(db, row, phone_id)
    tel, digits = _clean_phone(phone)
    if tel == p.phone:
        return _view(db, row)
    dup = db.scalar(select(ContactPhone.id).where(ContactPhone.contact_id == row.id, ContactPhone.digits == digits, ContactPhone.id != p.id))
    if dup is not None:
        raise invalid("phone_invalid", "This contact already has that number.")
    was_primary = _keys.phone_digits(row.phone) == p.digits
    p.phone, p.digits = tel, digits
    if was_primary:
        row.phone = tel
    db.flush()
    _note_change(db, row, "Phone changed", "phone_changed", actor, now)
    return _view(db, row)


def remove_phone(db: Session, contact_id: int, phone_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    p = _phone_of(db, row, phone_id)
    was_primary = _keys.phone_digits(row.phone) == p.digits
    db.delete(p)
    db.flush()
    if was_primary:
        rest = _phone_rows(db, row)
        row.phone = rest[0].phone if rest else None
        db.flush()
    _note_change(db, row, "Phone removed", "phone_removed", actor, now)
    return _view(db, row)


def set_primary_phone(db: Session, contact_id: int, phone_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    p = _phone_of(db, row, phone_id)
    if _keys.phone_digits(row.phone) == p.digits:
        return _view(db, row)
    row.phone = p.phone
    db.flush()
    _note_change(db, row, "Primary phone changed", "phone_primary", actor, now)
    return _view(db, row)


def add_tag(
    db: Session, contact_id: int, name: str, category: str = "custom", *, actor: Actor = TEAM, now: datetime | None = None
) -> ContactView:
    row = _get(db, contact_id)
    clean = _keys.collapse(name)
    if not 1 <= len(clean) <= 40 or category not in TAG_CATEGORIES:
        raise invalid("tag_invalid", "A tag is 1-40 characters.")
    key = _keys.name_key(clean)
    tag = db.scalar(select(Tag).where(Tag.name_key == key))
    if tag is None:
        tag = Tag(name=clean, name_key=key, category=category, created_at=now_or(now))
        db.add(tag)
        db.flush()
    if db.get(ContactTag, (row.id, tag.id)) is None:
        db.add(ContactTag(contact_id=row.id, tag_id=tag.id))
        db.flush()
        _rebuild_search_text(db, row)
        timeline.add_event(db, subject_type="contact", subject_id=row.id, kind="note", body=f"Tag added: {tag.name}",
                           data={**_AUTO_NOTE, "tag": tag.name, "change": "tag_added"}, actor=actor, occurred_at=now)
    return _view(db, row)


def remove_tag(db: Session, contact_id: int, tag_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    row = _get(db, contact_id)
    link = db.get(ContactTag, (row.id, tag_id))
    if link is not None:
        tag = db.get(Tag, tag_id)
        db.delete(link)
        db.flush()
        _rebuild_search_text(db, row)
        timeline.add_event(db, subject_type="contact", subject_id=row.id, kind="note",
                           body=f"Tag removed: {tag.name if tag else tag_id}",
                           data={**_AUTO_NOTE, "tag_id": tag_id, "change": "tag_removed"}, actor=actor, occurred_at=now)
    return _view(db, row)


def list_tags(db: Session) -> list[TagView]:
    rows = db.execute(
        select(Tag, func.count(ContactTag.contact_id))
        .outerjoin(ContactTag, ContactTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(Tag.name_key)
    ).all()
    return [TagView(id=t.id, name=t.name, category=t.category, count=int(n)) for t, n in rows]


def link(db: Session, contact_id: int, entity_type: str, entity_id: int, role: str | None = None) -> None:
    """Idempotent. A first link records ``contact_linked`` on the contact's own timeline only."""
    if entity_type not in LINK_ENTITY_TYPES:
        raise ValueError(f"unknown link entity {entity_type!r}")
    existing = db.scalar(
        select(ContactLink).where(
            ContactLink.contact_id == contact_id, ContactLink.entity_type == entity_type, ContactLink.entity_id == entity_id
        )
    )
    if existing is not None:
        if role and not existing.role:
            existing.role = role
            db.flush()
        return
    db.add(ContactLink(contact_id=contact_id, entity_type=entity_type, entity_id=entity_id, role=role, created_at=now_or(None)))
    db.flush()
    timeline.add_event(
        db, subject_type="contact", subject_id=contact_id, kind="contact_linked",
        body=f"Linked to {entity_type} #{entity_id}" + (f" as {role}" if role else ""),
        data={"entity_type": entity_type, "entity_id": entity_id, "role": role}, contact_id=contact_id,
    )


# ---------------------------------------------------------------------------- duplicates & merge
def find_duplicates(db: Session, *, limit: int = 50) -> list[DuplicateGroup]:
    groups: list[tuple[str, list[int]]] = []
    seen_sets: set[frozenset[int]] = set()

    def add(reason: str, ids: list[int]) -> None:
        fs = frozenset(ids)
        if len(fs) >= 2 and fs not in seen_sets:
            seen_sets.add(fs)
            groups.append((reason, sorted(fs)))

    for col, reason in ((Contact.email, "Same email"), (Contact.linkedin_slug, "Same LinkedIn profile")):
        dup_values = select(col).where(col.is_not(None)).group_by(col).having(func.count() > 1).limit(limit)
        for value in db.scalars(dup_values):
            add(reason, list(db.scalars(select(Contact.id).where(col == value))))
    # shared phone (last 10 digits) AND loosely matching names
    dup_digits = (
        select(ContactPhone.digits).group_by(ContactPhone.digits).having(func.count(func.distinct(ContactPhone.contact_id)) > 1).limit(limit * 4)
    )
    for digits in db.scalars(dup_digits):
        cids = sorted(set(db.scalars(select(ContactPhone.contact_id).where(ContactPhone.digits == digits))))
        names = dict(db.execute(select(Contact.id, Contact.full_name).where(Contact.id.in_(cids))).all())
        clusters: list[list[int]] = []
        for cid in cids:
            for cl in clusters:
                if any(_keys.names_match_loosely(names.get(cid), names.get(o)) for o in cl):
                    cl.append(cid)
                    break
            else:
                clusters.append([cid])
        for cl in clusters:
            add("Same phone number", cl)
    # identical casefolded name + company
    name_expr = func.lower(func.trim(Contact.full_name))
    dup_names = (
        select(name_expr, Contact.company_id)
        .where(Contact.full_name.is_not(None), func.trim(Contact.full_name) != "")
        .group_by(name_expr, Contact.company_id)
        .having(func.count() > 1)
        .limit(limit * 4)
    )
    for nm, comp_id in db.execute(dup_names).all():
        stmt = select(Contact.id).where(name_expr == nm)
        stmt = stmt.where(Contact.company_id == comp_id) if comp_id is not None else stmt.where(Contact.company_id.is_(None))
        add("Same name and company", list(db.scalars(stmt)))
    groups = groups[:limit]
    all_ids = sorted({i for _, ids in groups for i in ids})
    by_id = {c.id: c for c in db.scalars(select(Contact).where(Contact.id.in_(all_ids)))} if all_ids else {}
    views = {v.id: v for v in views_for(db, by_id.values())}
    return [DuplicateGroup(reason=r, contacts=[views[i] for i in ids if i in views]) for r, ids in groups]


def merge(db: Session, keep_id: int, drop_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ContactView:
    if keep_id == drop_id:
        raise invalid("merge_self", "Choose two different contacts to merge.")
    keep, drop = _get(db, keep_id), _get(db, drop_id)
    if keep.id == drop.id:
        raise invalid("merge_self", "Choose two different contacts to merge.")
    keep_id, drop_id = keep.id, drop.id
    stamp = now_or(now)
    # emails (globally unique addresses): just re-own them
    keep_has_primary = bool(db.scalar(select(ContactEmail.id).where(ContactEmail.contact_id == keep.id, ContactEmail.is_primary.is_(True))))
    db.execute(update(ContactEmail).where(ContactEmail.contact_id == drop.id).values(contact_id=keep.id, is_primary=False))
    if not keep_has_primary and keep.email:
        db.execute(update(ContactEmail).where(ContactEmail.contact_id == keep.id, ContactEmail.email == keep.email).values(is_primary=True))
    # phones: move the ones keep does not have
    have = set(db.scalars(select(ContactPhone.digits).where(ContactPhone.contact_id == keep.id)))
    for ph in list(db.scalars(select(ContactPhone).where(ContactPhone.contact_id == drop.id))):
        if ph.digits in have:
            db.delete(ph)
        else:
            ph.contact_id = keep.id
            have.add(ph.digits)
    # tags
    have_tags = set(db.scalars(select(ContactTag.tag_id).where(ContactTag.contact_id == keep.id)))
    for tid in set(db.scalars(select(ContactTag.tag_id).where(ContactTag.contact_id == drop.id))) - have_tags:
        db.add(ContactTag(contact_id=keep.id, tag_id=tid))
    # links
    keep_links = {(ln.entity_type, ln.entity_id): ln for ln in db.scalars(select(ContactLink).where(ContactLink.contact_id == keep.id))}
    for ln in list(db.scalars(select(ContactLink).where(ContactLink.contact_id == drop.id))):
        mine = keep_links.get((ln.entity_type, ln.entity_id))
        if mine is not None:
            if not mine.role and ln.role:
                mine.role = ln.role
            db.delete(ln)
        else:
            ln.contact_id = keep.id
    db.flush()
    for model in (Prospect, Enquiry, TimelineEvent):
        db.execute(update(model).where(model.contact_id == drop.id).values(contact_id=keep.id))
    # earlier merges into the dropped contact now point at the survivor; its own events move too
    db.execute(update(TimelineEvent).where(TimelineEvent.subject_type == "contact", TimelineEvent.subject_id == drop.id).values(subject_id=keep.id))

    # fill keep's empty columns from drop
    slug, slug_url = drop.linkedin_slug, drop.linkedin_url
    drop.linkedin_slug = None
    db.flush()
    kept_slug = bool(slug and not keep.linkedin_slug)
    if kept_slug:
        keep.linkedin_slug, keep.linkedin_url = slug, slug_url
    elif slug and slug != keep.linkedin_slug:
        # keep has its own profile: the other one must not be lost - show it in the notes (and alias it below)
        line = f"Also on LinkedIn: {slug_url or slug}"
        if line not in (keep.notes or ""):
            keep.notes = (keep.notes + "\n\n" + line) if keep.notes else line
    for attr in ("full_name", "email", "phone", "title", "company_id"):
        if not getattr(keep, attr) and getattr(drop, attr):
            setattr(keep, attr, getattr(drop, attr))
    if drop.notes:
        keep.notes = (keep.notes + "\n\n" + drop.notes) if keep.notes else drop.notes
    keep.updated_at = stamp
    dropped_name = drop.full_name or drop.email
    db.expunge(drop)
    db.execute(delete(Contact).where(Contact.id == drop.id))
    db.expire_all()
    keep = _get(db, keep_id)
    _rebuild_search_text(db, keep)
    timeline.add_event(
        db, subject_type="contact", subject_id=keep.id, kind="contact_merged",
        body=f"Merged duplicate contact {dropped_name or '#' + str(drop_id)} into this one.",
        data={"dropped_id": drop_id, "dropped_name": dropped_name, "dropped_slugs": [slug] if slug else [],
              "dropped_linkedin_url": slug_url, "kept_dropped_slug": kept_slug},
        actor=actor, occurred_at=stamp,
    )
    return _view(db, keep)


__all__ = [
    "add_email", "add_phone", "remove_email", "remove_phone", "set_primary_email", "set_primary_phone", "update_email",
    "update_phone",
    "add_tag", "find_duplicates", "resolve_id", "get_contact", "link", "list_contacts", "list_tags", "merge", "remove_tag",
    "update_contact", "upsert", "upsert_row", "views_for",
]

"""Sheet record -> CRM rows: the ONLY place that knows the mapping in spec/data.md.

``apply_record`` writes one parsed sheet row into the CRM inside the caller's session (it
flushes, never commits). Ownership contract (data.md -> "Sheet-fed tables"):

* SHEET-OWNED columns are overwritten by a new non-null value; a value that was non-null in the
  previous overlay and is null now clears the column; null -> null leaves it alone.
* CRM-owned data (timeline, AI columns, flags, follow-ups, tags, notes, stages a human set, ...)
  is never touched.
* Every create / update / restore writes a timeline event (actor ``sheet-sync``) that lists
  FIELD NAMES only, never cell values.
* No reminder is ever created here.

Origins: ``"sheet"`` (the sync: a ``SheetSource`` is given, stage_source ``sheet``) and
``"import"`` (CSV import: no source, actor ``import``; a record whose key already belongs to a
sheet-fed row is NOT written).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, is_dataclass
from dataclasses import asdict as dc_asdict
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc
from sales0.config import get_settings
from sales0.db.models import Enquiry, EnquirySource, LinkedinProfile
from sales0.db.models.contacts import Contact, ContactEmail, ContactPhone
from sales0.db.models.crm import Client, Company, Lead, Prospect
from sales0.db.models.registries import ChannelGroup, Competitor
from sales0.db.models.sheetdata import SheetSource as SheetSourceRow
from sales0.db.models.timeline import TimelineEvent
from sales0.scraper.urls import profile_slug
from sales0.services._common import clip
from sales0.services.dedupe import content_hash, normalize_text
from sales0.services.sheetsync import values as v
from sales0.services.sheetsync.records import (
    SheetCompetitor,
    SheetDiscarded,
    SheetEnquiry,
    SheetGroup,
    SheetMetricRow,
    SheetRecord,
    _Person,
)
from sales0.services.sheetsync.repository import SheetSource
from sales0.services.sheetsync.status_map import (
    LEAD_OUTCOME_STAGES,
    StatusOutcome,
    map_enquiry_status,
    map_person_status,
    norm_status,
)

if TYPE_CHECKING:
    from sales0.services.crm.actor import Actor

ORIGINS = ("sheet", "import")
OPEN_LEAD_STAGES = ("meeting_scheduled", "proposal_sent")
PLATFORMS = ("whatsapp", "linkedin", "telegram", "facebook", "other")
PLATFORM_ALIASES = {"fb": "facebook", "wa": "whatsapp", "li": "linkedin", "tg": "telegram", "whats app": "whatsapp"}
NO_TEXT = "(no text in the sheet row)"


@dataclass
class ApplyResult:
    action: str  # created | updated | restored | unchanged
    changed_fields: tuple[str, ...]
    target_type: str
    target_id: int


# ----------------------------------------------------------------------------- JSON-space helpers
def jsonify(value: Any) -> Any:
    """Make a record field JSON-safe (dates -> ISO, tuples -> lists, week dataclasses -> dicts)."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [jsonify(x) for x in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {k: jsonify(x) for k, x in dc_asdict(value).items()}
    if isinstance(value, dict):
        return {k: jsonify(x) for k, x in value.items()}
    return value


def json_fields(record: SheetRecord) -> dict[str, Any]:
    return {k: jsonify(val) for k, val in record.sheet_fields().items()}


def blank(value: Any) -> bool:
    """None, an empty list/tuple or an empty string: 'the cell had nothing'."""
    return value is None or (isinstance(value, list | tuple | str) and len(value) == 0)


def overlay(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Later parts win only where non-blank (the repository docstring's overlay rule)."""
    merged: dict[str, Any] = {}
    for part in parts:
        for name, value in part.items():
            if not blank(value):
                merged[name] = value
            else:
                merged.setdefault(name, None)
    return merged


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def local_midnight_utc(d: date | None) -> datetime | None:
    """A sheet date (no time) as local-midnight in the configured timezone, stored UTC."""
    if d is None:
        return None
    return datetime.combine(d, time.min, tzinfo=get_settings().tz).astimezone(UTC)


def key_text(value: str | None) -> str:
    """``lower(trim(...))`` with inner whitespace collapsed (the ``*_key`` column form)."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def first(value: Any) -> str | None:
    if isinstance(value, list | tuple):
        return str(value[0]) if value else None
    return None if value is None else str(value)


# ----------------------------------------------------------------------------- writer
class Writer:
    """Applies sheet-owned fields to one ORM row, remembering which columns changed."""

    def __init__(self, obj: Any, old: dict[str, Any], new: dict[str, Any]) -> None:
        self.obj, self.old, self.new = obj, old, new
        self.changed: list[str] = []

    def pull(self, column: str, field: str | None, convert: Callable[[Any], Any] = lambda x: x, *, only_if: bool = True) -> None:
        """Set ``column`` from overlay field ``field``; clear it if the field vanished from the sheet."""
        if not only_if:
            return
        name = field or column
        self._apply(column, self.new.get(name), self.old.get(name), convert)

    def pull_derived(self, column: str, new_value: Any, old_value: Any, convert: Callable[[Any], Any] = lambda x: x) -> None:
        self._apply(column, new_value, old_value, convert)

    def _apply(self, column: str, new: Any, old: Any, convert: Callable[[Any], Any]) -> None:
        if not blank(new):
            value = convert(new)
            if value is None:
                return
        elif not blank(old):
            value = None  # the cell was emptied in the sheet: clear
        else:
            return
        if getattr(self.obj, column) != value:
            setattr(self.obj, column, value)
            self.changed.append(column)

    def set(self, column: str, value: Any) -> None:
        """Set a non-overlay column (state transitions)."""
        if getattr(self.obj, column) != value:
            setattr(self.obj, column, value)
            self.changed.append(column)


class Ctx:
    """Everything one apply call needs."""

    def __init__(self, db: Session, *, origin: str, source: SheetSource | None, actor: "Actor", now: datetime) -> None:
        if origin not in ORIGINS:
            raise ValueError(f"unknown origin {origin!r}")
        self.db, self.origin, self.source, self.actor, self.now = db, origin, source, actor, ensure_utc(now)

    @property
    def is_sheet(self) -> bool:
        return self.origin == "sheet"

    @property
    def stage_source(self) -> str:
        return "sheet" if self.is_sheet else "app"

    def event(
        self,
        subject_type: str,
        subject_id: int,
        kind: str,
        body: str | None = None,
        data: dict[str, Any] | None = None,
        *,
        prospect_id: int | None = None,
        enquiry_id: int | None = None,
        company_id: int | None = None,
        contact_id: int | None = None,
    ) -> None:
        self.db.add(
            TimelineEvent(
                subject_type=subject_type, subject_id=subject_id, kind=kind, body=body, data=data,
                prospect_id=prospect_id, enquiry_id=enquiry_id, company_id=company_id, contact_id=contact_id,
                actor=self.actor.label[:80], actor_member_id=getattr(self.actor, "member_id", None),
                occurred_at=self.now,
            )
        )


# ----------------------------------------------------------------------------- overlays
def _sources_of(db: Session, target_type: str, target_id: int) -> list[SheetSourceRow]:
    return list(db.scalars(select(SheetSourceRow).where(SheetSourceRow.target_type == target_type, SheetSourceRow.target_id == target_id)))


def overlays(
    ctx: Ctx, record: SheetRecord, target_type: str, target_id: int | None, previous: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(old_overlay, new_overlay, this_tab_fields)`` for the target, in JSON space.

    The overlay of several tabs follows the repository docstring: tabs in ``(sheet_id, gid)``
    order, later tabs winning only where non-None. ``old`` uses this tab's stored snapshot, ``new``
    this tab's fresh fields; every other tab's snapshot is shared.
    """
    mine = json_fields(record)
    if target_id is None or not ctx.is_sheet or ctx.source is None:
        old = dict(previous or {})
        return old, (overlay([old, mine]) if old else dict(mine)), mine
    this_key = (ctx.source.sheet_id, ctx.source.gid)
    others: list[tuple[tuple[str, str], dict[str, Any]]] = []
    own_old: dict[str, Any] | None = None
    rows = _sources_of(ctx.db, target_type, target_id)
    for row in rows:
        k = (row.sheet_id, row.gid)
        if k == this_key:
            if row.entity == record.ENTITY and row.key == record.key:
                own_old = dict(row.fields or {})
            continue
        others.append((k, dict(row.fields or {})))

    def ordered(this: dict[str, Any] | None) -> list[dict[str, Any]]:
        items = list(others)
        if this is not None:
            items.append((this_key, this))
        items.sort(key=lambda kv: kv[0])
        return [f for _, f in items]

    old = overlay(ordered(own_old))
    if previous is not None and not rows:
        old = dict(previous)
    return old, overlay(ordered(mine)), mine


# ----------------------------------------------------------------------------- shared lookups
def company_for(db: Session, name: str | None, *, website: str | None = None) -> Company | None:
    name = clip(name, 200)
    if not name:
        return None
    key = key_text(name)
    row = db.scalar(select(Company).where(Company.name_key == key))
    if row is None:
        row = Company(name=name, name_key=key)
        db.add(row)
        db.flush()
    if website and not row.website:
        wk = v.website_key(website)
        row.website = clip(website, 300)
        row.website_key = clip(wk, 200)
        db.flush()
    return row


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-10:]


def contact_for(
    ctx: Ctx,
    *,
    name: str | None,
    emails: list[str] | tuple[str, ...] = (),
    phones: list[str] | tuple[str, ...] = (),
    linkedin_url: str | None = None,
    company_id: int | None = None,
) -> Contact | None:
    """Upsert the shared person through the CRM contacts service, then attach every extra address."""
    name = clip(name, 200)
    emails = [e.lower() for e in emails if e]
    if not (name or emails or profile_slug(linkedin_url)):
        return None
    from sales0.services.crm import contacts as crm_contacts

    view = crm_contacts.upsert(
        ctx.db, full_name=name, email=emails[0] if emails else None, phone=phones[0] if phones else None,
        linkedin_url=linkedin_url if profile_slug(linkedin_url) else None, company_id=company_id,
        origin="sheet" if ctx.is_sheet else "import", actor=ctx.actor, now=ctx.now,
    )
    contact = ctx.db.get(Contact, view.id)
    if contact is None:  # pragma: no cover - the service just created it
        return None
    for extra in emails[1:]:
        if ctx.db.scalar(select(ContactEmail.id).where(ContactEmail.email == extra)) is None:
            ctx.db.add(ContactEmail(contact_id=contact.id, email=extra, is_primary=False))
    have = {p.digits for p in ctx.db.scalars(select(ContactPhone).where(ContactPhone.contact_id == contact.id))}
    for phone in phones:
        d = _digits(phone)
        if d and d not in have:
            ctx.db.add(ContactPhone(contact_id=contact.id, phone=phone[:40], digits=d))
            have.add(d)
    ctx.db.flush()
    return contact


def _target_by_key(ctx: Ctx, record: SheetRecord, target_type: str, entities: tuple[str, ...]) -> int | None:
    """An already-synced target for this natural key (any tab), if any."""
    return ctx.db.scalar(
        select(SheetSourceRow.target_id)
        .where(SheetSourceRow.key == record.key, SheetSourceRow.entity.in_(entities), SheetSourceRow.target_type == target_type)
        .order_by(SheetSourceRow.id)
        .limit(1)
    )


def _event_body(prefix: str, changed: list[str]) -> str:
    names = ", ".join(sorted(set(changed)))
    return f"{prefix}: {names}" if names else prefix


# ----------------------------------------------------------------------------- entry point
def apply_record(
    db: Session,
    record: SheetRecord,
    *,
    origin: str,
    source: SheetSource | None,
    actor: "Actor",
    now: datetime,
    previous_fields: dict[str, Any] | None = None,
) -> ApplyResult:
    ctx = Ctx(db, origin=origin, source=source, actor=actor, now=now)
    if isinstance(record, SheetMetricRow):
        from sales0.services.sheetsync.metrics import apply_metric

        if not ctx.is_sheet:
            raise ValueError("metric rows are only applied by the sheet sync")
        return apply_metric(db, record, now=now)
    if ctx.origin == "import" and _owned_by_sheet(ctx, record):
        return ApplyResult("unchanged", (), _target_type_of(record), 0)
    if isinstance(record, SheetEnquiry):  # SheetDiscarded is a subclass
        return _apply_enquiry(ctx, record, previous_fields)
    if isinstance(record, _Person):
        return _apply_person(ctx, record, previous_fields)
    if isinstance(record, SheetCompetitor):
        return _apply_competitor(ctx, record, previous_fields)
    if isinstance(record, SheetGroup):
        return _apply_group(ctx, record, previous_fields)
    raise TypeError(f"cannot apply {type(record).__name__}")


def _target_type_of(record: SheetRecord) -> str:
    return {"enquiry": "enquiry", "discarded": "enquiry", "prospect": "prospect", "competitor": "competitor", "group": "channel_group"}[
        record.ENTITY
    ]


def _owned_by_sheet(ctx: Ctx, record: SheetRecord) -> bool:
    """CSV import must not overwrite a row the sheet feeds (same entity family + key)."""
    entities = ("enquiry", "discarded") if record.ENTITY in ("enquiry", "discarded") else (record.ENTITY,)
    return _target_by_key(ctx, record, _target_type_of(record), entities) is not None


def _finish(ctx: Ctx, record: SheetRecord, obj: Any, target_type: str, created: bool, restored: bool, changed: list[str], subject: str, scopes: dict[str, Any], tab_name: str = "") -> ApplyResult:
    changed = sorted(set(changed))
    if created:
        action, kind = "created", ("sheet_created" if ctx.is_sheet else "import")
        body = f"Created from the sheet ({tab_name})" if ctx.is_sheet else "Created by CSV import"
    elif restored:
        action, kind, body = "restored", "sheet_restored", "Back in the sheet; restored"
    elif changed:
        action, kind = "updated", ("sheet_updated" if ctx.is_sheet else "import")
        body = _event_body("Updated from the sheet" if ctx.is_sheet else "Merged by CSV import", changed)
    else:
        return ApplyResult("unchanged", (), target_type, obj.id)
    ctx.event(subject, obj.id, kind, body, {"fields": changed} if changed else None, **scopes)
    ctx.db.flush()
    return ApplyResult(action, tuple(changed), target_type, obj.id)


# ============================================================================ enquiries
ENQUIRY_ENTITIES = ("enquiry", "discarded")


def _find_enquiry(ctx: Ctx, rec: SheetEnquiry) -> Enquiry | None:
    db = ctx.db
    tid = _target_by_key(ctx, rec, "enquiry", ENQUIRY_ENTITIES)
    if tid is not None:
        row = db.get(Enquiry, tid)
        if row is not None:
            return row
    # Adoption (never a duplicate): same post, then same text.
    post = rec.post_url
    if post:
        key = rec.key if rec.key_source == "post" else ""
        if key.startswith("post:"):
            act = key.split(":", 1)[1]
            by_ref = db.scalar(
                select(EnquirySource.enquiry_id).where(or_(EnquirySource.source_ref.contains(act), EnquirySource.source_url.contains(act))).limit(1)
            )
            if by_ref is not None:
                return db.get(Enquiry, by_ref)
            by_url = db.scalar(select(Enquiry.id).where(Enquiry.source_url.contains(act)).order_by(Enquiry.id).limit(1))
            if by_url is not None:
                return db.get(Enquiry, by_url)
        else:
            by_url = db.scalar(select(Enquiry.id).where(Enquiry.source_url == post).order_by(Enquiry.id).limit(1))
            if by_url is not None:
                return db.get(Enquiry, by_url)
    digest = _hash_for(rec)
    by_hash = db.scalar(select(Enquiry.id).where(Enquiry.content_hash == digest))
    return db.get(Enquiry, by_hash) if by_hash is not None else None


def _hash_for(rec: SheetEnquiry) -> str:
    text = rec.full_enquiry
    if text and normalize_text(text):
        return content_hash(text)
    return hashlib.sha256(f"sheet:{rec.key}".encode()).hexdigest()


def _has_active_source(db: Session, enquiry_id: int) -> bool:
    return db.scalar(
        select(func.count(SheetSourceRow.id)).where(
            SheetSourceRow.target_type == "enquiry", SheetSourceRow.target_id == enquiry_id, SheetSourceRow.entity == "enquiry"
        )
    ) > 0


def _apply_enquiry(ctx: Ctx, rec: SheetEnquiry, previous: dict[str, Any] | None) -> ApplyResult:
    db = ctx.db
    discarded = isinstance(rec, SheetDiscarded)
    enquiry = _find_enquiry(ctx, rec)
    created = enquiry is None
    restored = False
    old_o, new_o, _mine = overlays(ctx, rec, "enquiry", None if enquiry is None else enquiry.id, previous)
    changed: list[str] = []
    if enquiry is None:
        text = rec.full_enquiry or rec.requirement_summary or NO_TEXT
        enquiry = Enquiry(
            source="linkedin_post", content_hash=_hash_for(rec), raw_text=text, origin="sheet" if ctx.is_sheet else "import",
            review_status="discarded" if discarded else "accepted", triage_status="done",
            stage=None if discarded else "prospect_new", decided_at=ctx.now,
            date_added=local_midnight_utc(rec.date_added) or ctx.now,
        )
        db.add(enquiry)
        db.flush()
    elif ctx.is_sheet and enquiry.sheet_removed_at is not None:
        restored = True
        enquiry.sheet_removed_at = None
        changed.append("sheet_removed_at")

    own = enquiry.origin in ("sheet", "import")
    w = Writer(enquiry, old_o, new_o)
    w.pull("date_posted", "date_posted", _as_date)
    if own and not created:
        w.pull("date_added", "date_added", lambda x: local_midnight_utc(_as_date(x)))
    w.pull("requirement_summary", None, lambda x: clip(x, 140))
    w.pull("topic", "summary_topic", lambda x: clip(x, 300))
    w.pull("city", "summary_city", lambda x: clip(x, 100))
    w.pull("mode", "engagement_mode", lambda x: x if x in ("online", "offline", "hybrid") else None)
    w.pull_derived("company", new_o.get("company") or new_o.get("summary_company"), old_o.get("company") or old_o.get("summary_company"), lambda x: clip(x, 200))
    w.pull("email", "emails", lambda x: clip(first(x), 254))
    w.pull("priority", None, lambda x: x if x in ("high", "medium", "low") else None)
    w.pull("sheet_status", None, lambda x: clip(x, 200))
    w.pull("sheet_notes", None)
    w.pull("date_email_sent", None, _as_date)
    w.pull("date_proposal_sent", None, _as_date)
    w.pull("phone", "phones", lambda x: clip(first(x), 50))
    w.pull("source_url", "post_url", lambda x: clip(x, 1000))
    w.pull("contact_person", None, lambda x: clip(x, 200))
    if own:
        if not created:
            w.pull("raw_text", "full_enquiry", lambda x: x if normalize_text(x) else None)
        w.pull("update_note", "sheet_update")
    if discarded:
        w.pull_derived(
            "discard_reason", new_o.get("discard_reason") or new_o.get("sheet_status"),
            old_o.get("discard_reason") or old_o.get("sheet_status"), lambda x: clip(x, 200),
        )
    changed += w.changed
    if ctx.is_sheet:
        enquiry.sheet_fields = new_o

    # review status: the sheet decides for rows it owns (or still-pending ones); a human's call stands.
    may_decide = own or enquiry.review_status == "pending_review"
    flipped_to_discarded = False
    if may_decide and not created:
        if not discarded and enquiry.review_status != "accepted":
            enquiry.review_status, enquiry.decided_at, enquiry.stage = "accepted", ctx.now, "prospect_new"
            if enquiry.triage_status == "pending" and own:
                enquiry.triage_status = "done"
            changed.append("review_status")
        elif discarded and enquiry.review_status == "accepted" and own and not _has_active_source(db, enquiry.id):
            enquiry.review_status, enquiry.decided_at, enquiry.stage = "discarded", ctx.now, None
            changed.append("review_status")
            flipped_to_discarded = True
    db.flush()

    tab = ctx.source.tab_name if ctx.source else ""
    scopes = {"enquiry_id": enquiry.id, "prospect_id": enquiry.prospect_id, "company_id": enquiry.company_id}
    result = _finish(ctx, rec, enquiry, "enquiry", created, restored, changed, "enquiry", scopes, tab)

    if enquiry.review_status == "accepted" and not discarded:
        extra = _link_prospect(ctx, rec, enquiry, new_o, old_o)
        if extra and result.action == "unchanged":
            result = ApplyResult("updated", tuple(extra), "enquiry", enquiry.id)
        elif extra:
            result = ApplyResult(result.action, tuple(sorted(set(result.changed_fields) | set(extra))), "enquiry", enquiry.id)
    elif flipped_to_discarded:
        _close_discarded_prospect(ctx, enquiry)
    return result


def _close_discarded_prospect(ctx: Ctx, enquiry: Enquiry) -> None:
    prospect = ctx.db.get(Prospect, enquiry.prospect_id) if enquiry.prospect_id else None
    if prospect is None or prospect.stage != "new" or prospect.status != "open" or prospect.stage_source == "app" and prospect.contacted_at:
        return
    other = ctx.db.scalar(
        select(func.count(Enquiry.id)).where(Enquiry.prospect_id == prospect.id, Enquiry.id != enquiry.id, Enquiry.review_status == "accepted")
    )
    if other:
        return
    prospect.status, prospect.closed_reason, prospect.closed_at = "closed", "enquiry_discarded", ctx.now
    ctx.event("prospect", prospect.id, "stage_change", "Closed: the sheet moved the enquiry to Discarded",
              {"closed_reason": "enquiry_discarded"}, prospect_id=prospect.id, enquiry_id=enquiry.id, company_id=prospect.company_id)


# ---- the prospect side of an accepted enquiry --------------------------------------------------
def _untouched(p: Prospect) -> bool:
    return p.stage == "new" and p.status == "open" and p.contacted_at is None and p.responded_at is None


def _sheet_may_move(p: Prospect) -> bool:
    if p.stage_source == "sheet":
        return p.status != "closed" or p.closed_reason is None or p.closed_reason.startswith("sheet:") or p.closed_reason == "no_response"
    return _untouched(p)


def _link_prospect(ctx: Ctx, rec: SheetEnquiry, enquiry: Enquiry, new_o: dict[str, Any], old_o: dict[str, Any]) -> list[str]:
    from sales0.services.crm import prospects as crm_prospects

    db = ctx.db
    had_prospect = enquiry.prospect_id is not None
    crm_prospects.ensure_for_enquiry(db, enquiry.id, actor=ctx.actor, now=ctx.now)
    db.refresh(enquiry)
    prospect = db.get(Prospect, enquiry.prospect_id) if enquiry.prospect_id else None
    if prospect is None:
        return []
    extra: list[str] = []
    if not had_prospect and prospect.stage_source != "sheet" and ctx.is_sheet and _untouched(prospect):
        prospect.stage_source = "sheet"
    # extra e-mails / phones of the cell, and the contact's profile link
    emails, phones = list(new_o.get("emails") or []), list(new_o.get("phones") or [])
    contact = db.get(Contact, enquiry.contact_id) if enquiry.contact_id else None
    if contact is None and (emails or rec.contact_person):
        company = company_for(db, enquiry.company)
        contact = contact_for(ctx, name=enquiry.contact_person, emails=emails, phones=phones, company_id=company.id if company else None)
        if contact is not None:
            enquiry.contact_id = contact.id
            if prospect.contact_id is None:
                prospect.contact_id = contact.id
    elif contact is not None:
        for e in emails:
            if db.scalar(select(ContactEmail.id).where(ContactEmail.email == e.lower())) is None:
                db.add(ContactEmail(contact_id=contact.id, email=e.lower(), is_primary=False))
        have = {p.digits for p in db.scalars(select(ContactPhone).where(ContactPhone.contact_id == contact.id))}
        for ph in phones:
            d = _digits(ph)
            if d and d not in have:
                db.add(ContactPhone(contact_id=contact.id, phone=ph[:40], digits=d))
                have.add(d)
    link = new_o.get("contact_link")
    if contact is not None and link and profile_slug(link) and not contact.linkedin_slug:
        taken = db.scalar(select(Contact.id).where(Contact.linkedin_slug == profile_slug(link)))
        if taken is None:
            contact.linkedin_slug, contact.linkedin_url = profile_slug(link), clip(link if link.startswith("http") else "https://" + link, 500)
    db.flush()

    # status -> stage
    sheet_status = enquiry.sheet_status
    outcome = map_enquiry_status(
        sheet_status, date_email_sent=enquiry.date_email_sent, date_proposal_sent=enquiry.date_proposal_sent
    )
    extra += _apply_outcome(ctx, prospect, outcome, enquiry=enquiry, sheet_status=sheet_status, date_added=_as_date(new_o.get("date_added")))
    return extra


def _apply_outcome(
    ctx: Ctx,
    prospect: Prospect,
    outcome: StatusOutcome,
    *,
    enquiry: Enquiry | None,
    sheet_status: str | None,
    date_added: date | None = None,
) -> list[str]:
    """Move the prospect / lead per the status tables, unless a human set the stage."""
    db = ctx.db
    scopes = {"prospect_id": prospect.id, "company_id": prospect.company_id, "enquiry_id": enquiry.id if enquiry else None}
    changed: list[str] = []
    if not _sheet_may_move(prospect):
        if _would_change(db, prospect, outcome):
            _log_unmoved_status(ctx, prospect, sheet_status, outcome, scopes)
        return changed

    def stage_change(frm: str, to: str, note: str | None = None) -> None:
        ctx.event("prospect", prospect.id, "stage_change", note or f"Stage set from the sheet status ({outcome.rule})",
                  {"from": frm, "to": to, "rule": outcome.rule}, **scopes)

    if ctx.is_sheet:
        prospect.stage_source = "sheet"
    # reopen a prospect the sheet itself closed when the sheet status moved on
    if outcome.prospect_status == "open" and prospect.status == "closed":
        prospect.status, prospect.closed_reason, prospect.closed_at = "open", None, None
        changed.append("prospect_status")
    if outcome.prospect_stage is not None and prospect.stage != outcome.prospect_stage:
        old = prospect.stage
        prospect.stage = outcome.prospect_stage
        stage_change(old, prospect.stage)
        changed.append("prospect_stage")
    keep_lead = prospect.status == "converted" and _has_sheet_lead(db, prospect)
    if outcome.prospect_status == "closed" and prospect.status == "open":
        prospect.status, prospect.closed_reason, prospect.closed_at = "closed", outcome.closed_reason, ctx.now
        changed.append("prospect_status")
        ctx.event("prospect", prospect.id, "stage_change", f"Closed from the sheet status ({outcome.rule})",
                  {"status": "closed", "reason": outcome.closed_reason, "rule": outcome.rule}, **scopes)
    elif outcome.prospect_status == "closed" and not keep_lead and prospect.status == "closed" and prospect.closed_reason != "enquiry_discarded" and prospect.closed_reason != outcome.closed_reason:
        prospect.closed_reason = outcome.closed_reason
    if (outcome.contacted or prospect.stage in ("follow_up_1", "follow_up_2", "final_follow_up")) and prospect.contacted_at is None:
        sent = enquiry.date_email_sent if enquiry else None
        prospect.contacted_at = local_midnight_utc(sent) or ctx.now
        prospect.contacted_estimated = True
        changed.append("contacted_at")
    if outcome.lead_stage is not None:
        changed += _apply_lead(ctx, prospect, outcome, enquiry, date_added, scopes)
    db.flush()
    _log_unmoved_status(ctx, prospect, sheet_status, outcome, scopes, moved=True)
    return changed


def _would_change(db: Session, prospect: Prospect, outcome: StatusOutcome) -> bool:
    """Would applying ``outcome`` alter the prospect / lead? (An unchanged re-import must stay silent.)"""
    if outcome.unknown:
        return True
    if outcome.prospect_stage is not None and prospect.stage != outcome.prospect_stage:
        return True
    if outcome.lead_stage is not None:
        lead = db.scalar(select(Lead).where(Lead.prospect_id == prospect.id).order_by(Lead.closed_at.is_(None).desc(), Lead.id.desc()).limit(1))
        return lead is None or lead.stage != outcome.lead_stage
    if outcome.prospect_status == "closed":
        return prospect.status != "closed"
    return False


def _log_unmoved_status(ctx: Ctx, prospect: Prospect, sheet_status: str | None, outcome: StatusOutcome, scopes: dict[str, Any], *, moved: bool = False) -> None:
    """`sheet_status` timeline event: once per distinct unknown value, or when a human's stage was kept."""
    raw = (sheet_status or "").strip()
    if not raw:
        return
    if moved and not outcome.unknown:
        return
    seen = ctx.db.scalars(
        select(TimelineEvent).where(TimelineEvent.subject_type == "prospect", TimelineEvent.subject_id == prospect.id, TimelineEvent.kind == "sheet_status")
    )
    tag = norm_status(raw)
    for e in seen:
        if (e.data or {}).get("status_key") == tag:
            return
    body = "Sheet status not recognised (treated as New)" if outcome.unknown else "Sheet status changed; the stage you set in the app was kept"
    ctx.event("prospect", prospect.id, "sheet_status", body, {"status": raw[:200], "status_key": tag, "unknown": outcome.unknown}, **scopes)


def _has_sheet_lead(db: Session, prospect: Prospect) -> bool:
    return db.scalar(select(func.count(Lead.id)).where(Lead.prospect_id == prospect.id, Lead.stage_source == "sheet")) > 0


def _apply_lead(ctx: Ctx, prospect: Prospect, outcome: StatusOutcome, enquiry: Enquiry | None, date_added: date | None, scopes: dict[str, Any]) -> list[str]:
    db = ctx.db
    target = outcome.lead_stage
    assert target is not None
    sent = enquiry.date_email_sent if enquiry else None
    proposal = enquiry.date_proposal_sent if enquiry else None
    opened = local_midnight_utc(sent) or local_midnight_utc(date_added) or ctx.now
    closing = target in LEAD_OUTCOME_STAGES
    lead = db.scalar(select(Lead).where(Lead.prospect_id == prospect.id).order_by(Lead.closed_at.is_(None).desc(), Lead.id.desc()).limit(1))
    changed: list[str] = []
    if lead is None:
        lead = Lead(
            prospect_id=prospect.id, company_id=prospect.company_id, stage=target, stage_source=ctx.stage_source,
            opened_at=opened, closed_at=(local_midnight_utc(proposal) or ctx.now) if closing else None,
            proposal_sent_on=proposal, opened_by="sheet" if ctx.is_sheet else "manual",
            actor=ctx.actor.label[:80], actor_member_id=getattr(ctx.actor, "member_id", None),
        )
        db.add(lead)
        db.flush()
        ctx.event("lead", lead.id, "lead_created", f"Lead opened from the sheet status ({outcome.rule})", {"stage": target}, **scopes)
        changed.append("lead")
    elif lead.stage_source == "sheet" and lead.stage != target:
        old = lead.stage
        lead.stage = target
        lead.closed_at = (local_midnight_utc(proposal) or ctx.now) if closing else None
        if proposal and target == "proposal_sent":
            lead.proposal_sent_on = proposal
        ctx.event("lead", lead.id, "lead_stage", f"Lead stage updated from the sheet ({old} -> {target})", {"from": old, "to": target}, **scopes)
        changed.append("lead_stage")
    elif lead.stage_source == "app":
        return changed  # a human owns this lead
    prospect.status = "converted"
    prospect.closed_reason, prospect.closed_at = None, None
    if prospect.stage == "new":
        prospect.stage = "contacted"
    if prospect.contacted_at is None:
        prospect.contacted_at, prospect.contacted_estimated = lead.opened_at, True
    if prospect.responded_at is None and target != "no_response":
        prospect.responded_at = lead.opened_at  # a no-response lead is contact without a response
    if target == "won":
        client = db.scalar(select(Client).where(Client.lead_id == lead.id))
        if client is None:
            company = db.get(Company, prospect.company_id) if prospect.company_id else None
            client = Client(
                name=(company.name if company else prospect.name)[:200], company_id=prospect.company_id, status="active", source="sheet",
                lead_id=lead.id, prospect_id=prospect.id, started_on=proposal or ctx.now.date(),
                actor=ctx.actor.label[:80], actor_member_id=getattr(ctx.actor, "member_id", None),
            )
            db.add(client)
            db.flush()
            ctx.event("client", client.id, "client_created", "Client created from the sheet status (won)", {"lead_id": lead.id}, **scopes)
            changed.append("client")
    db.flush()
    return changed


# ============================================================================ prospects (people)
def _profile_for(ctx: Ctx, rec: _Person) -> LinkedinProfile | None:
    slug = profile_slug(rec.linkedin_url) or (f"in/{rec.linkedin_slug}" if rec.linkedin_slug else None)
    if not slug:
        return None
    db = ctx.db
    row = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == slug))
    if row is None:
        row = LinkedinProfile(
            slug=slug, kind="company" if slug.startswith("company/") else "person", profile_url=f"https://www.linkedin.com/{slug}/"[:500],
            display_name=clip(rec.name, 200), first_seen_at=ctx.now, last_seen_at=ctx.now, posts_seen=0, relevant_count=0,
        )
        db.add(row)
        db.flush()
    return row


def _find_prospect(ctx: Ctx, rec: _Person, profile: LinkedinProfile | None) -> Prospect | None:
    db = ctx.db
    tid = _target_by_key(ctx, rec, "prospect", ("prospect",))
    if tid is not None:
        row = db.get(Prospect, tid)
        if row is not None:
            return row
    if profile is not None:
        return db.scalar(select(Prospect).where(Prospect.linkedin_profile_id == profile.id))
    if rec.name:
        # no profile: the key is name + company; find the same pair among sheet people (covers a removed-then-returned row)
        q = select(Prospect).where(Prospect.linkedin_profile_id.is_(None), Prospect.source == "sheet", func.lower(Prospect.name) == key_text(rec.name))
        for cand in db.scalars(q.order_by(Prospect.id)):
            cname = db.scalar(select(Company.name_key).where(Company.id == cand.company_id)) if cand.company_id else ""
            if (cname or "") == key_text(rec.company):
                return cand
    return None


def _apply_person(ctx: Ctx, rec: _Person, previous: dict[str, Any] | None) -> ApplyResult:
    db = ctx.db
    profile = _profile_for(ctx, rec)
    prospect = _find_prospect(ctx, rec, profile)
    created, restored = prospect is None, False
    old_o, new_o, _mine = overlays(ctx, rec, "prospect", None if prospect is None else prospect.id, previous)
    changed: list[str] = []
    company = company_for(db, new_o.get("company"), website=new_o.get("website"))
    if prospect is None:
        prospect = Prospect(
            kind="outreach_person", name=clip(rec.name or rec.company or "(unnamed)", 200) or "(unnamed)",
            linkedin_profile_id=profile.id if profile else None, company_id=company.id if company else None, stage="new",
            stage_source=ctx.stage_source, status="open", source="sheet" if ctx.is_sheet else "import",
            actor=ctx.actor.label[:80], actor_member_id=getattr(ctx.actor, "member_id", None),
        )
        db.add(prospect)
        db.flush()
        if profile is not None and profile.tracked is None:
            profile.tracked = True
    elif ctx.is_sheet and prospect.sheet_removed_at is not None:
        restored = True
        prospect.sheet_removed_at = None
        changed.append("sheet_removed_at")
    if profile is not None and prospect.linkedin_profile_id is None:
        taken = db.scalar(select(Prospect.id).where(Prospect.linkedin_profile_id == profile.id))
        if taken is None:
            prospect.linkedin_profile_id = profile.id
            changed.append("linkedin_url")
    w = Writer(prospect, old_o, new_o)
    if new_o.get("name") is not None:
        w.pull("name", None, lambda x: clip(x, 200))
    w.pull("sheet_status", None, lambda x: clip(x, 200))
    w.pull("sheet_notes", None)
    w.pull("reason_url", None, lambda x: clip(x, 1000))
    w.pull("reason_text", None, lambda x: clip(x, 500))
    w.pull("extra_link", None, lambda x: clip(x, 1000))
    changed += w.changed
    if company is not None and prospect.company_id != company.id:
        prospect.company_id = company.id
        changed.append("company")
    if ctx.is_sheet:
        prospect.sheet_fields = new_o
    # shared person row
    if prospect.contact_id is None:
        contact = contact_for(ctx, name=new_o.get("name"), linkedin_url=new_o.get("linkedin_url"), company_id=company.id if company else None)
        if contact is not None and prospect.contact_id != contact.id and prospect.contact_id is None:
            prospect.contact_id = contact.id
    db.flush()

    scopes = {"prospect_id": prospect.id, "company_id": prospect.company_id}
    status = prospect.sheet_status
    outcome = map_person_status(status)
    moved = _apply_outcome(ctx, prospect, outcome, enquiry=None, sheet_status=status)
    changed += moved
    tab = ctx.source.tab_name if ctx.source else ""
    result = _finish(ctx, rec, prospect, "prospect", created, restored, changed, "prospect", scopes, tab)
    return result


# ============================================================================ competitors
def _apply_competitor(ctx: Ctx, rec: SheetCompetitor, previous: dict[str, Any] | None) -> ApplyResult:
    db = ctx.db
    row: Competitor | None = None
    tid = _target_by_key(ctx, rec, "competitor", ("competitor",))
    if tid is not None:
        row = db.get(Competitor, tid)
    if row is None and rec.website_key:
        row = db.scalar(select(Competitor).where(Competitor.website_key == rec.website_key))
    if row is None and rec.name:
        row = db.scalar(select(Competitor).where(Competitor.name_key == key_text(rec.name)))
    created, restored = row is None, False
    old_o, new_o, _ = overlays(ctx, rec, "competitor", None if row is None else row.id, previous)
    changed: list[str] = []
    if row is None:
        name = clip(rec.name or rec.website_key or rec.key, 200) or rec.key
        if db.scalar(select(Competitor.id).where(Competitor.name_key == key_text(name))) is not None:
            name = clip(f"{name} ({rec.website_key or 'sheet'})", 200) or name
        row = Competitor(name=name, name_key=key_text(name), active=True)
        db.add(row)
        db.flush()
    elif ctx.is_sheet and row.sheet_removed_at is not None:
        restored = True
        row.sheet_removed_at = None
        changed.append("sheet_removed_at")
    w = Writer(row, old_o, new_o)
    w.pull("website", None, lambda x: clip(x, 300))
    if new_o.get("website_key") and row.website_key != new_o["website_key"]:
        taken = db.scalar(select(Competitor.id).where(Competitor.website_key == new_o["website_key"], Competitor.id != row.id))
        if taken is None:
            w.pull("website_key", None, lambda x: clip(x, 200))
    if new_o.get("name") and key_text(new_o["name"]) != row.name_key:
        taken = db.scalar(select(Competitor.id).where(Competitor.name_key == key_text(new_o["name"]), Competitor.id != row.id))
        if taken is None:
            w.pull("name", None, lambda x: clip(x, 200))
            row.name_key = key_text(row.name)
    w.pull("approx_clients", None, lambda x: int(x))
    w.pull("client_names", None)
    w.pull("contact", None, lambda x: clip(x, 300))
    w.pull("email", "emails", lambda x: clip(first(x), 254))
    w.pull("emails", None, lambda x: list(x) or None)
    w.pull("sheet_notes", None)
    changed += w.changed
    if ctx.is_sheet:
        row.sheet_fields = new_o
    db.flush()
    tab = ctx.source.tab_name if ctx.source else ""
    return _finish(ctx, rec, row, "competitor", created, restored, changed, "competitor", {}, tab)


# ============================================================================ groups
def _platform(value: str | None) -> str:
    p = key_text(value)
    p = PLATFORM_ALIASES.get(p, p)
    if not p:
        return "whatsapp"
    return p if p in PLATFORMS else "other"


def _apply_group(ctx: Ctx, rec: SheetGroup, previous: dict[str, Any] | None) -> ApplyResult:
    db = ctx.db
    link_key = rec.key.removeprefix("link:") if rec.key.startswith("link:") else (v.normalize_url(rec.link) if rec.link else None)
    platform = _platform(rec.platform)
    row: ChannelGroup | None = None
    tid = _target_by_key(ctx, rec, "channel_group", ("group",))
    if tid is not None:
        row = db.get(ChannelGroup, tid)
    if row is None and link_key:
        row = db.scalar(select(ChannelGroup).where(ChannelGroup.link_key == link_key))
    if row is None and rec.name:
        row = db.scalar(select(ChannelGroup).where(ChannelGroup.platform == platform, ChannelGroup.name_key == key_text(rec.name), ChannelGroup.link_key.is_(None)))
    created, restored = row is None, False
    old_o, new_o, _ = overlays(ctx, rec, "channel_group", None if row is None else row.id, previous)
    changed: list[str] = []
    if row is None:
        name = clip(rec.name or link_key or rec.key, 200) or rec.key
        row = ChannelGroup(name=name, name_key=key_text(name), platform=platform, active=True)
        db.add(row)
        db.flush()
    elif ctx.is_sheet and row.sheet_removed_at is not None:
        restored = True
        row.sheet_removed_at = None
        changed.append("sheet_removed_at")
    w = Writer(row, old_o, new_o)
    if new_o.get("name"):
        w.pull("name", None, lambda x: clip(x, 200))
        row.name_key = key_text(row.name)
    w.set("platform", platform)
    w.pull("link", None, lambda x: clip(x, 500))
    if link_key and row.link_key != link_key:
        taken = db.scalar(select(ChannelGroup.id).where(ChannelGroup.link_key == clip(link_key, 500), ChannelGroup.id != row.id))
        if taken is None:
            w.set("link_key", clip(link_key, 500))
    elif not new_o.get("link") and old_o.get("link") and row.link_key is not None:
        w.set("link_key", None)
    w.pull("details", None, lambda x: clip(x, 200))
    w.pull("keep_level", None, lambda x: {"keep": "keep", "keep-low": "keep_low"}.get(x))
    changed += w.changed
    if ctx.is_sheet:
        row.sheet_fields = new_o
    db.flush()
    tab = ctx.source.tab_name if ctx.source else ""
    return _finish(ctx, rec, row, "channel_group", created, restored, changed, "group", {}, tab)


__all__ = ["ApplyResult", "apply_record", "company_for", "contact_for", "jsonify", "json_fields", "overlay"]

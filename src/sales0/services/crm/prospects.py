"""Prospects: one per LinkedIn profile (else per enquiry / sheet row / manual entry)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sales0.db.models import (
    Client,
    Company,
    Contact,
    ContactEmail,
    ContactLink,
    Enquiry,
    Flag,
    FollowUp,
    Lead,
    LinkedinProfile,
    OutreachMessage,
    OutreachReply,
    Prospect,
    SheetSource,
    TeamMember,
)
from sales0.config import get_settings
from sales0.db.models.enums_crm import PROSPECT_STAGES
from sales0.errors import DomainError
from sales0.scraper.urls import link_kind_of
from sales0.services import profiles
from sales0.services._common import clip, now_or, not_found
from sales0.services.crm import _keys, characteristics, companies, contacts, flags, followups, timeline
from sales0.services.crm._common import UNSET, invalid, page_params, today_local
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.stages import stage_label, stage_rank
from sales0.services.crm.views import (
    ClientView,
    LinkedEnquiryView,
    LinkedMessageView,
    LinkedReplyView,
    ProspectDetail,
    ProspectPage,
    ProspectView,
    SheetSourceView,
)

UNTRACK_REASONS = ("no_response", "lost", "not_interested")


# ---------------------------------------------------------------------------- views
def linked_enquiry_view(e: Enquiry) -> LinkedEnquiryView:
    return LinkedEnquiryView(
        id=e.id, requirement_summary=e.requirement_summary, priority=e.priority, review_status=e.review_status,
        ai_verdict=e.ai_verdict, date_added=e.date_added, source_url=e.source_url, company=e.company, sheet_status=e.sheet_status,
        prospect_id=e.prospect_id, author_name=e.author_name, author_profile_url=e.author_profile_url,
        link_kind=link_kind_of(e.source_url),
    )


def views_for(db: Session, rows: Iterable[Prospect]) -> list[ProspectView]:
    rows = list(rows)
    if not rows:
        return []
    ids = [r.id for r in rows]
    cids = {r.company_id for r in rows if r.company_id}
    contact_ids = {r.contact_id for r in rows if r.contact_id}
    prof_ids = {r.linkedin_profile_id for r in rows if r.linkedin_profile_id}
    company_names = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(cids))).all()) if cids else {}
    contact_names = (
        {cid: (n or em) for cid, n, em in db.execute(select(Contact.id, Contact.full_name, Contact.email).where(Contact.id.in_(contact_ids))).all()}
        if contact_ids else {}
    )
    profs = {p.id: p for p in db.scalars(select(LinkedinProfile).where(LinkedinProfile.id.in_(prof_ids)))} if prof_ids else {}
    flag_map = flags.flags_for(db, "prospect", ids)
    traits = characteristics.for_prospects(db, ids)
    open_leads = dict(db.execute(select(Lead.prospect_id, Lead.id).where(Lead.prospect_id.in_(ids), Lead.closed_at.is_(None))).all())
    tabs: dict[int, list[str]] = defaultdict(list)
    for tid, tab in db.execute(
        select(SheetSource.target_id, SheetSource.tab_name).where(SheetSource.target_type == "prospect", SheetSource.target_id.in_(ids)).order_by(SheetSource.id)
    ).all():
        if tab not in tabs[tid]:
            tabs[tid].append(tab)
    for pid, tab in db.execute(
        select(Enquiry.prospect_id, SheetSource.tab_name)
        .join(SheetSource, (SheetSource.target_type == "enquiry") & (SheetSource.target_id == Enquiry.id))
        .where(Enquiry.prospect_id.in_(ids))
        .order_by(SheetSource.id)
    ).all():
        if tab not in tabs[pid]:
            tabs[pid].append(tab)
    out: list[ProspectView] = []
    for r in rows:
        prof = profs.get(r.linkedin_profile_id) if r.linkedin_profile_id else None
        out.append(
            ProspectView(
                id=r.id, name=r.name, kind=r.kind, stage=r.stage, stage_label=stage_label(r.stage), status=r.status,
                closed_reason=r.closed_reason, source=r.source, company_id=r.company_id,
                company_name=company_names.get(r.company_id), contact_id=r.contact_id, contact_name=contact_names.get(r.contact_id),
                linkedin_profile_id=r.linkedin_profile_id, linkedin_url=prof.profile_url if prof else None,
                tracked=prof.tracked if prof else None, contacted_at=r.contacted_at, contacted_estimated=r.contacted_estimated,
                responded_at=r.responded_at, next_follow_up_on=r.next_follow_up_on, flags=flag_map.get(r.id, []),
                sheet_status=r.sheet_status, sheet_removed_at=r.sheet_removed_at, sheet_tabs=tabs.get(r.id, []),
                open_lead_id=open_leads.get(r.id), created_at=r.created_at, characteristics=traits.get(r.id, []),
            )
        )
    return out


def _view(db: Session, row: Prospect) -> ProspectView:
    return views_for(db, [row])[0]


def _get(db: Session, prospect_id: int) -> Prospect:
    row = db.get(Prospect, prospect_id)
    if row is None:
        raise not_found()
    return row


def _stamp_actor(p: Prospect, actor: Actor) -> None:
    p.actor, p.actor_member_id = actor.label[:80], actor.member_id


def _require_open(p: Prospect) -> None:
    if p.status == "closed":
        raise DomainError("prospect_closed", "This prospect is closed - reopen it first.", 409)
    if p.status == "converted":
        raise DomainError("prospect_closed", "This prospect is already a lead - move the lead instead.", 409)


# ---------------------------------------------------------------------------- stage engine
def _set_stage(
    db: Session, p: Prospect, stage: str, *, actor: Actor, now: datetime | None, note: str | None = None,
    when: datetime | None = None, extra: dict | None = None, schedule: bool = True, due_dates: dict | None = None,
) -> bool:
    """Move ``p`` to ``stage`` (any direction). Returns False when nothing changed."""
    stamp = now_or(now)
    prev = p.stage
    first_contact = False
    if stage != "new" and p.contacted_at is None:
        p.contacted_at, p.contacted_estimated, first_contact = (when or stamp), False, True
    elif stage == "contacted" and p.contacted_estimated and when is not None:
        p.contacted_at, p.contacted_estimated = when, False
    if prev == stage and not first_contact:
        return False
    p.stage, p.stage_source = stage, "app"
    _stamp_actor(p, actor)
    p.updated_at = stamp
    db.flush()
    if prev != stage:
        body = f"Stage: {stage_label(prev)} -> {stage_label(stage)}" + (f" - {note.strip()}" if note and note.strip() else "")
        timeline.add_event(
            db, subject_type="prospect", subject_id=p.id, kind="stage_change", body=body,
            data={"from": prev, "to": stage, **({"note": note.strip()} if note and note.strip() else {}), **(extra or {})},
            actor=actor, occurred_at=stamp,
        )
    if first_contact and schedule:
        followups.schedule_for_contact(db, p.id, p.contacted_at, now=stamp, due_dates=due_dates)  # type: ignore[arg-type]
    if stage != "new":
        followups.complete_up_to(db, p.id, stage, actor=actor, now=stamp)
    followups.refresh_next(db, p.id)
    return True


def move_stage(
    db: Session, prospect_id: int, stage: str, *, actor: Actor = TEAM, note: str | None = None, now: datetime | None = None
) -> ProspectView:
    if stage not in PROSPECT_STAGES:
        raise invalid("stage_invalid", "Choose a valid stage.")
    p = _get(db, prospect_id)
    _require_open(p)
    _set_stage(db, p, stage, actor=actor, now=now, note=note)
    return _view(db, p)


def advance_to(
    db: Session, prospect_id: int, stage: str, *, actor: Actor, now: datetime | None, reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """Move forward only (never back), and only an open prospect; used by completed reminders."""
    p = db.get(Prospect, prospect_id)
    if p is None or p.status != "open" or stage not in PROSPECT_STAGES or stage_rank(stage) <= stage_rank(p.stage):
        return
    _set_stage(db, p, stage, actor=actor, now=now, note=reason, extra=extra)


def mark_contacted(
    db: Session, prospect_id: int, *, when: datetime | None = None, actor: Actor = TEAM, channel: str = "manual",
    note: str | None = None, now: datetime | None = None, due_dates: dict | None = None,
) -> ProspectView:
    p = _get(db, prospect_id)
    _require_open(p)
    stamp = now_or(now)
    if p.stage == "new":
        _set_stage(db, p, "contacted", actor=actor, now=stamp, note=note, when=when, extra={"channel": channel}, due_dates=due_dates)
    elif p.contacted_at is None or p.contacted_estimated:
        p.contacted_at, p.contacted_estimated, p.updated_at = (when or stamp), False, stamp
        db.flush()
        timeline.add_event(db, subject_type="prospect", subject_id=p.id, kind="stage_change",
                           body="Contact date recorded" + (f" - {note.strip()}" if note and note.strip() else ""),
                           data={"channel": channel, "contacted_at": p.contacted_at.isoformat()}, actor=actor, occurred_at=stamp)
        followups.schedule_for_contact(db, p.id, p.contacted_at, now=stamp, due_dates=due_dates)
    return _view(db, p)


def log_contact(
    db: Session, prospect_id: int, *, channel: str, note: str | None = None, contacted_on: date | None = None,
    due_dates: dict | None = None, actor: Actor = TEAM, now: datetime | None = None,
) -> ProspectView:
    """The FIRST contact made by hand (WhatsApp / LinkedIn / Email / Call): New -> Contacted, the contact date recorded
    and the three follow-ups scheduled (the agent's dates, else contact day + offsets). The stage_change timeline entry
    carries the channel and the note."""
    ch = followups.check_channel(channel, required=True)
    p = _get(db, prospect_id)
    _require_open(p)
    if p.stage != "new" and p.contacted_at is not None and not p.contacted_estimated:
        raise invalid("already_contacted", "This prospect is already contacted - mark its follow-ups done instead.")
    stamp = now_or(now)
    tz = get_settings().tz
    today = today_local(stamp)
    day = contacted_on or today
    if not isinstance(day, date) or day > today or day.year < 2000:
        raise invalid("contact_invalid", "The contact date must be today or earlier.")
    when = stamp if day == today else datetime.combine(day, time(12, 0), tzinfo=tz).astimezone(UTC)
    for kind, value in (due_dates or {}).items():
        if value is not None and value < day:
            raise invalid("followup_invalid", f"{followups.KIND_LABELS.get(kind, kind)} cannot be before the contact date.")
    return mark_contacted(
        db, p.id, when=when, actor=actor, channel=ch or "manual", note=(note or "").strip() or None, now=stamp,
        due_dates={k: v for k, v in (due_dates or {}).items() if v is not None} or None,
    )  # fmt: skip


def record_email_contact(
    db: Session, prospect_id: int, *, when: datetime, actor: Actor, schedule: bool = True, now: datetime | None = None,
) -> bool:
    """A real email went out to ``prospect_id`` whatever its status (closed / converted included).

    Records ``contacted_at`` (a real date replaces an estimated one) and moves a ``new`` stage to ``contacted``;
    the status never changes. Reminders are scheduled only when ``schedule`` and the prospect is open.
    Returns True when anything was recorded.
    """
    p = _get(db, prospect_id)
    stamp = now_or(now)
    if p.contacted_at is not None and not p.contacted_estimated and p.stage != "new":
        return False
    if p.status == "open":
        if p.stage == "new":
            return _set_stage(db, p, "contacted", actor=actor, now=stamp, when=when, extra={"channel": "email"}, schedule=schedule)
        p.contacted_at, p.contacted_estimated, p.updated_at = when, False, stamp
        db.flush()
        timeline.add_event(db, subject_type="prospect", subject_id=p.id, kind="stage_change", body="Contact date recorded",
                           data={"channel": "email", "contacted_at": when.isoformat()}, actor=actor, occurred_at=stamp)
        if schedule:
            followups.schedule_for_contact(db, p.id, when, now=stamp)
        return True
    if p.contacted_at is None or p.contacted_estimated:
        p.contacted_at, p.contacted_estimated = when, False
    prev = p.stage
    if prev == "new":
        p.stage, p.stage_source = "contacted", "app"
    p.updated_at = stamp
    db.flush()
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="stage_change",
        body=f"Email sent while the prospect is {p.status}: contact date recorded"
        + (f", stage {stage_label(prev)} -> {stage_label(p.stage)}" if prev != p.stage else "") + "; status unchanged, no reminders scheduled.",
        data={"channel": "email", "contacted_at": p.contacted_at.isoformat(), "status": p.status,
              **({"from": prev, "to": p.stage} if prev != p.stage else {})},
        actor=actor, occurred_at=stamp,
    )
    return True


def record_response(
    db: Session, prospect_id: int, *, actor: Actor = TEAM, source: str = "manual", reply_id: int | None = None,
    note: str | None = None, now: datetime | None = None, data: dict | None = None,
) -> ProspectView:
    p = _get(db, prospect_id)
    stamp = now_or(now)
    if p.responded_at is None:
        p.responded_at = stamp
    p.updated_at = stamp
    db.flush()
    followups.cancel_open(db, prospect_id=p.id, reason="response_received", actor=actor, now=stamp)
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="reply_received",
        body="Response received" + (f" - {note.strip()}" if note and note.strip() else ""),
        data={"source": source, **({"reply_id": reply_id} if reply_id is not None else {}), **(data or {})},
        actor=actor, occurred_at=stamp,
    )
    return _view(db, p)


def _set_profile_tracked(db: Session, p: Prospect, tracked: bool) -> bool | None:
    """Set the profile flag; returns the previous value (None if no profile / unchanged sentinel)."""
    if not p.linkedin_profile_id:
        return None
    prof = db.get(LinkedinProfile, p.linkedin_profile_id)
    if prof is None or prof.tracked is tracked:
        return None
    prof.tracked = tracked
    db.flush()
    return True


def close(db: Session, prospect_id: int, reason: str, *, actor: Actor = TEAM, now: datetime | None = None) -> ProspectView:
    p = _get(db, prospect_id)
    why = clip(reason, 60)
    if not why:
        raise invalid("reason_invalid", "Give a reason for closing this prospect.")
    if p.status == "closed":
        return _view(db, p)
    if p.status == "converted":
        raise DomainError("prospect_closed", "This prospect is already a lead - close the lead instead.", 409)
    stamp = now_or(now)
    p.status, p.closed_reason, p.closed_at, p.updated_at = "closed", why, stamp, stamp
    _stamp_actor(p, actor)
    db.flush()
    untracked = why in UNTRACK_REASONS and bool(_set_profile_tracked(db, p, False))
    followups.cancel_open(db, prospect_id=p.id, reason="prospect_closed", actor=actor, now=stamp)
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="stage_change", body=f"Closed: {why.replace('_', ' ')}",
        data={"status": "closed", "reason": why, **({"tracked": False} if untracked else {})}, actor=actor, occurred_at=stamp,
    )
    return _view(db, p)


def reopen(db: Session, prospect_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ProspectView:
    p = _get(db, prospect_id)
    if p.status != "closed":
        return _view(db, p)
    stamp = now_or(now)
    was = p.closed_reason
    p.status, p.closed_reason, p.closed_at, p.updated_at = "open", None, None, stamp
    _stamp_actor(p, actor)
    db.flush()
    retracked = was in UNTRACK_REASONS and bool(_set_profile_tracked(db, p, True))
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="stage_change", body="Reopened",
        data={"status": "open", "was": was, **({"tracked": True} if retracked else {})}, actor=actor, occurred_at=stamp,
    )
    return _view(db, p)


def set_tracked(db: Session, prospect_id: int, tracked: bool, *, actor: Actor = TEAM, now: datetime | None = None) -> ProspectView:
    p = _get(db, prospect_id)
    if not p.linkedin_profile_id:
        raise DomainError("tracked_unavailable", "This prospect has no LinkedIn profile to track.", 409)
    if _set_profile_tracked(db, p, bool(tracked)):
        timeline.add_event(
            db, subject_type="prospect", subject_id=p.id, kind="tracked_changed",
            body="Now tracked" if tracked else "No longer tracked", data={"tracked": bool(tracked)}, actor=actor, occurred_at=now,
        )
    return _view(db, p)


def update(
    db: Session, prospect_id: int, *, name=UNSET, notes=UNSET, company=UNSET, actor: Actor = TEAM,  # type: ignore[no-untyped-def]
    website=UNSET, company_linkedin_url=UNSET, linkedin_url=UNSET, now: datetime | None = None,  # type: ignore[no-untyped-def]
) -> ProspectView:
    """Edit the prospect's own fields; ``website`` / ``company_linkedin_url`` edit its company row and
    ``linkedin_url`` links (or, blank, unlinks) the person's LinkedIn profile - the prospect's identity."""
    p = _get(db, prospect_id)
    changed: list[str] = []
    if name is not UNSET:
        new = clip(_keys.collapse(name), 200)
        if not new:
            raise invalid("prospect_invalid", "A prospect needs a name.")
        if new != p.name:
            p.name = new
            changed.append("name")
    if notes is not UNSET and ((notes or "").strip() or None) != p.notes:
        p.notes = (notes or "").strip() or None
        changed.append("notes")
    if company is not UNSET:
        comp = companies.get_or_create_row(db, company, now=now) if company else None
        if (comp.id if comp else None) != p.company_id:
            p.company_id = comp.id if comp else None
            changed.append("company")
    company_fields = {k: v for k, v in (("website", website), ("linkedin_url", company_linkedin_url)) if v is not UNSET}
    if company_fields:
        if p.company_id is None:
            if any(company_fields.values()):
                raise invalid("prospect_invalid", "Add a company name first - the website belongs to the company.")
        else:
            before = companies._view(db.get(Company, p.company_id))  # noqa: SLF001
            after = companies.update_company(db, p.company_id, actor=actor, now=now, **company_fields)
            changed += [f"company {k}" for k in company_fields if getattr(before, k) != getattr(after, k)]
    if linkedin_url is not UNSET:
        changed += _relink_profile(db, p, linkedin_url, now=now)
    if changed:
        p.updated_at = now_or(now)
        _stamp_actor(p, actor)
        db.flush()
        timeline.add_event(db, subject_type="prospect", subject_id=p.id, kind="note", body="Details edited: " + ", ".join(changed),
                           data={"system": True, "fields": changed}, actor=actor, occurred_at=now)
    return _view(db, p)


def _relink_profile(db: Session, p: Prospect, url: str | None, *, now: datetime | None) -> list[str]:
    text = (url or "").strip()
    if not text:
        if p.linkedin_profile_id is None:
            return []
        p.linkedin_profile_id = None
        return ["linkedin profile"]
    slug = _keys.linkedin_slug_of(text)
    if not slug or not slug.startswith("in/"):
        raise invalid("prospect_invalid", "Use the person's LinkedIn profile link (linkedin.com/in/...).")
    profile_id = profiles.ensure_profile(db, f"https://www.linkedin.com/{slug}/", name=p.name, now=now)
    if profile_id == p.linkedin_profile_id:
        return []
    owner = db.scalar(select(Prospect.id).where(Prospect.linkedin_profile_id == profile_id, Prospect.id != p.id))
    if owner is not None:
        raise DomainError("prospect_invalid", f"That LinkedIn profile already belongs to prospect #{owner}.", 409)
    p.linkedin_profile_id = profile_id
    return ["linkedin profile"]


def add_contact(
    db: Session, prospect_id: int, *, full_name: str | None = None, email: str | None = None, phone: str | None = None,
    title: str | None = None, linkedin_url: str | None = None, actor: Actor = TEAM, now: datetime | None = None,
):  # type: ignore[no-untyped-def]
    """Add (or match) a person and link them to the prospect; the first one becomes its primary contact."""
    p = _get(db, prospect_id)
    if linkedin_url and not (_keys.linkedin_slug_of(linkedin_url) or "").startswith("in/"):
        raise invalid("contact_invalid", "Use the person's LinkedIn profile link (linkedin.com/in/...).")
    if email and not _keys.first_email(email):
        raise invalid("email_invalid", "That is not a valid e-mail address.")
    row, _ = contacts.upsert_row(
        db, full_name=full_name, email=email, phone=phone, title=title, linkedin_url=linkedin_url, company_id=p.company_id,
        origin="manual", actor=actor, now=now,
    )
    contacts.link(db, row.id, "prospect", p.id, None)
    if p.contact_id is None:
        p.contact_id = row.id
        db.flush()
    return contacts.views_for(db, [row])[0]


# ---------------------------------------------------------------------------- creation
def _contact_for(db: Session, p: Prospect, link_entity: tuple[str, int] | None, role: str, **kw) -> Contact | None:  # type: ignore[no-untyped-def]
    try:
        row, _ = contacts.upsert_row(db, **kw)
    except DomainError:
        return None
    contacts.link(db, row.id, "prospect", p.id, role)
    if link_entity:
        contacts.link(db, row.id, link_entity[0], link_entity[1], role)
    return row


def _source_of(e: Enquiry) -> str:
    return e.origin if e.origin in ("sheet", "import", "manual") else "enquiry"


def _by_origin(db: Session, enquiry_id: int) -> Prospect | None:
    return db.scalar(select(Prospect).where(Prospect.origin_enquiry_id == enquiry_id).order_by(Prospect.id).limit(1))


def ensure_for_enquiry(db: Session, enquiry_id: int, *, actor: Actor = TEAM, now: datetime | None = None) -> ProspectView:
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    stamp = now_or(now)
    comp = companies.get_or_create_row(db, e.company, now=stamp, actor=actor)
    if e.company_id is None and comp is not None:
        e.company_id = comp.id
    # --- already linked: heal missing pieces, reopen a discarded-closed prospect, change nothing else
    p = db.get(Prospect, e.prospect_id) if e.prospect_id else None
    if p is not None:
        if p.status == "closed" and p.closed_reason == "enquiry_discarded":
            reopen(db, p.id, actor=actor, now=stamp)
        if p.company_id is None and comp is not None:
            p.company_id = comp.id
        db.flush()
        return _view(db, p)
    # --- identity: one prospect per linkedin profile
    profile_id = e.author_profile_id
    if profile_id is None and e.author_profile_url:
        profile_id = profiles.ensure_profile(db, e.author_profile_url, name=e.author_name, now=stamp)
        if profile_id is not None:
            e.author_profile_id = profile_id
    prof = db.get(LinkedinProfile, profile_id) if profile_id else None
    p = db.scalar(select(Prospect).where(Prospect.linkedin_profile_id == profile_id)) if profile_id else None
    if p is None:
        # a profile-less enquiry has no other identity than itself: a prospect that already names it as its
        # origin (link lost, or a concurrent accept) is THE prospect (unique index uq_prospects_origin_enquiry)
        p = _by_origin(db, e.id)
    created = p is None
    if p is None:
        name = (e.company or "").strip() or (e.author_name or "").strip() or (e.contact_person or "").strip() or (
            (prof.display_name or "").strip() if prof else ""
        ) or "Unnamed prospect"
        p = Prospect(
            kind="company_enquiry", name=name[:200], linkedin_profile_id=profile_id, company_id=comp.id if comp else None,
            origin_enquiry_id=e.id, stage="new", stage_source="app", status="open", source=_source_of(e),
            actor=actor.label[:80], actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
        )
        try:
            with db.begin_nested():
                db.add(p)
                db.flush()
        except IntegrityError:
            # lost a race: another writer committed a prospect for this enquiry (or profile) between our read and
            # our insert. Take theirs instead of making a duplicate.
            if p in db:
                db.expunge(p)
            p = _by_origin(db, e.id) or (
                db.scalar(select(Prospect).where(Prospect.linkedin_profile_id == profile_id)) if profile_id else None
            )
            if p is None:
                raise
            created = False
    if not created:
        if p.company_id is None and comp is not None:
            p.company_id = comp.id
        if p.origin_enquiry_id is None:
            p.origin_enquiry_id = e.id
        if p.status == "closed" and p.closed_reason == "enquiry_discarded":
            reopen(db, p.id, actor=actor, now=stamp)
    e.prospect_id = p.id
    # --- contacts: the buyer named in the post and the LinkedIn author
    buyer = None
    if e.email or e.contact_person:
        buyer = _contact_for(db, p, ("enquiry", e.id), "buyer", full_name=e.contact_person, email=e.email, phone=e.phone,
                             company_id=comp.id if comp else None, origin="enquiry", actor=actor, now=stamp)
    author = None
    if prof is not None:
        author = _contact_for(db, p, ("enquiry", e.id), "author", full_name=e.author_name or prof.display_name,
                              linkedin_url=prof.profile_url, title=e.author_headline, company_id=None, origin="enquiry",
                              actor=actor, now=stamp)
    primary = buyer or author
    if primary is not None:
        e.contact_id = e.contact_id or primary.id
        if p.contact_id is None:
            p.contact_id = primary.id
    # --- tracking + timeline
    tracked_now = False
    if prof is not None and prof.tracked is None:
        prof.tracked, tracked_now = True, True
    db.flush()
    scope = {"prospect_id": p.id, "enquiry_id": e.id, "company_id": p.company_id}
    if created:
        timeline.add_event(
            db, subject_type="prospect", subject_id=p.id, kind="created",
            body="Prospect created from an accepted enquiry.",
            data={"enquiry_id": e.id, "source": p.source, **({"tracked": True} if tracked_now else {})},
            actor=actor, occurred_at=stamp, **{k: v for k, v in scope.items() if k != "enquiry_id"},
        )
    timeline.add_event(
        db, subject_type="prospect", subject_id=p.id, kind="enquiry_linked",
        body="Enquiry linked" + (f": {e.requirement_summary}" if e.requirement_summary else "."),
        data={"enquiry_id": e.id}, actor=actor, occurred_at=stamp, **scope,
    )
    flags.on_enquiry_linked(db, e.id, now=stamp)
    return _view(db, p)


def apply_accept_choices(
    db: Session, enquiry_id: int, *, characteristic_ids: Iterable[int] = (), as_client: bool = False,
    actor: Actor = TEAM, now: datetime | None = None,
) -> ProspectView:
    """The Inbox Accept form's extras, after the decision: tick characteristics on the enquiry's prospect and,
    for an enquiry from an existing client, take it straight to Client (through the lead -> client path)."""
    view = ensure_for_enquiry(db, enquiry_id, actor=actor, now=now)  # idempotent; the accept hook normally ran it
    ids = [int(i) for i in characteristic_ids]
    if ids:
        characteristics.add_to(db, "prospect", view.id, ids, actor=actor, now=now)
    if as_client:
        from sales0.services.crm import leads as leads_mod

        leads_mod.promote_to_client(db, view.id, actor=actor, note="Accepted from the Inbox as an existing client", now=now)
    return _view(db, _get(db, view.id))


def create_manual(
    db: Session, *, name: str, company: str | None = None, email: str | None = None, phone: str | None = None,
    linkedin_url: str | None = None, website: str | None = None, notes: str | None = None, source: str = "manual",
    actor: Actor = TEAM, now: datetime | None = None,
) -> ProspectView:
    clean = clip(_keys.collapse(name), 200)
    if not clean:
        raise invalid("prospect_invalid", "A prospect needs a name.")
    stamp = now_or(now)
    src = source if source in ("sheet", "manual", "import") else "manual"
    origin = {"sheet": "sheet", "import": "import"}.get(src, "manual")
    comp = companies.get_or_create_row(db, company, website=website, now=stamp)
    slug = _keys.linkedin_slug_of(linkedin_url)
    linkedin_url = f"https://www.linkedin.com/{slug}/" if slug else None
    profile_id = profiles.ensure_profile(db, linkedin_url, name=clean, now=stamp) if linkedin_url else None
    existing: Prospect | None = None
    if profile_id:
        existing = db.scalar(select(Prospect).where(Prospect.linkedin_profile_id == profile_id))
    contact_row = None
    if email or linkedin_url or phone:
        try:
            contact_row, _ = contacts.upsert_row(
                db, full_name=clean, email=email, phone=phone, linkedin_url=linkedin_url,
                company_id=comp.id if comp else None, origin=origin, actor=actor, now=stamp,
            )
        except DomainError:
            contact_row = None
    if existing is None and contact_row is not None and _keys.first_email(email):
        existing = db.scalar(
            select(Prospect).where(Prospect.contact_id == contact_row.id).order_by((Prospect.status == "open").desc(), Prospect.id).limit(1)
        )
    if existing is not None:
        if comp is not None and existing.company_id is None:
            existing.company_id = comp.id
        if contact_row is not None and existing.contact_id is None:
            existing.contact_id = contact_row.id
        if profile_id and existing.linkedin_profile_id is None:
            existing.linkedin_profile_id = profile_id
        db.flush()
        if contact_row is not None:
            contacts.link(db, contact_row.id, "prospect", existing.id, None)
        return _view(db, existing)
    p = Prospect(
        kind="company_enquiry" if comp is not None else "outreach_person", name=clean, linkedin_profile_id=profile_id,
        company_id=comp.id if comp else None, contact_id=contact_row.id if contact_row else None, stage="new",
        stage_source="app", status="open", source=src, notes=(notes or "").strip() or None, actor=actor.label[:80],
        actor_member_id=actor.member_id, created_at=stamp, updated_at=stamp,
    )
    db.add(p)
    db.flush()
    tracked_now = False
    if profile_id:
        prof = db.get(LinkedinProfile, profile_id)
        if prof is not None and prof.tracked is None:
            prof.tracked, tracked_now = True, True
    if contact_row is not None:
        contacts.link(db, contact_row.id, "prospect", p.id, "sheet" if src == "sheet" else None)
    db.flush()
    timeline.add_event(db, subject_type="prospect", subject_id=p.id, kind="created", body=f"Prospect added ({src}).",
                       data={"source": src, **({"tracked": True} if tracked_now else {})}, actor=actor, occurred_at=stamp)
    return _view(db, p)


# ---------------------------------------------------------------------------- reads
def get_prospect(db: Session, prospect_id: int) -> ProspectDetail:
    p = _get(db, prospect_id)
    base = _view(db, p)
    enq_rows = list(db.scalars(select(Enquiry).where(Enquiry.prospect_id == p.id).order_by(Enquiry.date_added.desc(), Enquiry.id.desc())))
    enq_ids = [e.id for e in enq_rows]
    conds = [OutreachMessage.prospect_id == p.id]
    if enq_ids:
        conds.append(OutreachMessage.enquiry_id.in_(enq_ids))
    msg_rows = list(db.execute(
        select(OutreachMessage, TeamMember.name).join(TeamMember, TeamMember.id == OutreachMessage.member_id)
        .where(or_(*conds)).order_by(OutreachMessage.created_at.desc(), OutreachMessage.id.desc())
    ).all())
    messages = [
        LinkedMessageView(id=m.id, subject=m.subject, state=m.state, sent_at=m.sent_at, reply_status=m.reply_status, owner_name=owner)
        for m, owner in msg_rows
    ]
    msg_ids = [m.id for m, _ in msg_rows]
    replies = [
        LinkedReplyView(id=r.id, message_id=r.message_id, received_at=r.received_at, kind=r.kind, intent=r.intent, snippet=r.snippet or None)
        for r in (db.scalars(select(OutreachReply).where(OutreachReply.message_id.in_(msg_ids)).order_by(OutreachReply.received_at.desc())) if msg_ids else [])
    ]
    contact_ids = list(dict.fromkeys(
        ([p.contact_id] if p.contact_id else [])
        + list(db.scalars(select(ContactLink.contact_id).where(ContactLink.entity_type == "prospect", ContactLink.entity_id == p.id).order_by(ContactLink.id)))
    ))
    contact_rows = {c.id: c for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids)))} if contact_ids else {}
    contact_views = contacts.views_for(db, [contact_rows[i] for i in contact_ids if i in contact_rows])
    lead_row = db.scalar(select(Lead).where(Lead.prospect_id == p.id).order_by(Lead.closed_at.is_(None).desc(), Lead.id.desc()).limit(1))
    lead_view = None
    client_view = None
    if lead_row is not None:
        from sales0.services.crm import leads as leads_mod

        lead_view = leads_mod.views_for(db, [lead_row])[0]
        c = db.scalar(select(Client).where(Client.lead_id == lead_row.id))
        client_view = _client_view(c) if c else None
    if client_view is None:
        c = db.scalar(select(Client).where(Client.prospect_id == p.id).order_by(Client.id.desc()).limit(1))
        client_view = _client_view(c) if c else None
    sources = [
        SheetSourceView(sheet_tab=s.tab_name, row_number=s.row_number, sheet_status=(s.fields or {}).get("status") if isinstance(s.fields, dict) else None,
                        last_seen_at=s.last_seen_at)
        for s in db.scalars(
            select(SheetSource).where(
                or_(
                    (SheetSource.target_type == "prospect") & (SheetSource.target_id == p.id),
                    (SheetSource.target_type == "enquiry") & (SheetSource.target_id.in_(enq_ids or [-1])),
                )
            ).order_by(SheetSource.id)
        )
    ]
    return ProspectDetail(
        **base.model_dump(), notes=p.notes, reason_url=p.reason_url, reason_text=p.reason_text, extra_link=p.extra_link,
        enquiries=[linked_enquiry_view(e) for e in enq_rows], messages=messages, replies=replies, contacts=contact_views,
        follow_ups=followups.for_prospect(db, p.id), timeline=timeline.list_timeline(db, prospect_id=p.id),
        lead=lead_view, client=client_view, sheet_sources=sources,
    )


def _client_view(c: Client) -> ClientView:
    from sales0.services.crm import clients as clients_mod

    db = Session.object_session(c)
    return clients_mod.views_for(db, [c])[0] if db is not None else clients_mod.view_of(c)


def list_prospects(
    db: Session, *, stage: str | None = None, status: str = "open", q: str | None = None, company_id: int | None = None,
    source: str | None = None, flag: str | None = None, due: str | None = None, characteristic_id: int | None = None,
    page: int = 1, page_size: int | None = None,
) -> ProspectPage:
    page, size = page_params(page, page_size)
    stmt = select(Prospect)
    if characteristic_id:
        stmt = stmt.where(characteristics.prospect_filter(characteristic_id))
    if status and status != "all":
        stmt = stmt.where(Prospect.status == status)
    if stage:
        stmt = stmt.where(Prospect.stage == stage)
    if company_id:
        stmt = stmt.where(Prospect.company_id == company_id)
    if source:
        stmt = stmt.where(Prospect.source == source)
    if q and q.strip():
        like = f"%{_keys.like_escape(_keys.name_key(q))}%"
        stmt = stmt.where(
            or_(
                func.lower(Prospect.name).like(like, escape="\\"),
                Prospect.company_id.in_(select(Company.id).where(Company.name_key.like(like, escape="\\"))),
                Prospect.contact_id.in_(select(Contact.id).where(Contact.search_text.like(like, escape="\\"))),
            )
        )
    if flag:
        stmt = stmt.where(exists().where(Flag.entity_type == "prospect", Flag.entity_id == Prospect.id, Flag.kind == flag, Flag.cleared_at.is_(None)))
    if due in ("today", "overdue"):
        today = today_local()
        cond = FollowUp.due_on == today if due == "today" else FollowUp.due_on < today
        stmt = stmt.where(exists().where(FollowUp.prospect_id == Prospect.id, FollowUp.done_at.is_(None), FollowUp.cancelled_at.is_(None), cond))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(Prospect.updated_at.desc(), Prospect.id.desc()).limit(size).offset((page - 1) * size)))
    return ProspectPage(items=views_for(db, rows), total=total, page=page, page_size=size)


def resolve_for_message(db: Session, message_id: int) -> int | None:
    m = db.get(OutreachMessage, message_id)
    if m is None:
        return None
    if m.prospect_id and db.get(Prospect, m.prospect_id) is not None:
        return m.prospect_id
    if m.enquiry_id:
        e = db.get(Enquiry, m.enquiry_id)
        if e is not None and e.prospect_id:
            return e.prospect_id
    addrs = _keys.all_emails(m.to_addrs)
    if addrs:
        cids = list(db.scalars(select(ContactEmail.contact_id).where(ContactEmail.email.in_(addrs))))
        if cids:
            linked = select(ContactLink.entity_id).where(ContactLink.entity_type == "prospect", ContactLink.contact_id.in_(cids))
            return db.scalar(
                select(Prospect.id)
                .where(or_(Prospect.contact_id.in_(cids), Prospect.id.in_(linked)))
                .order_by((Prospect.status == "open").desc(), Prospect.updated_at.desc(), Prospect.id.desc())
                .limit(1)
            )
    return None


def local_today(now: datetime | None = None) -> date:
    return today_local(now)


__all__ = [
    "SYSTEM", "add_contact", "advance_to", "apply_accept_choices", "close", "create_manual", "ensure_for_enquiry", "get_prospect",
    "linked_enquiry_view", "log_contact",
    "list_prospects", "mark_contacted", "move_stage", "record_email_contact", "record_response", "reopen", "resolve_for_message", "set_tracked",
    "update", "views_for",
]

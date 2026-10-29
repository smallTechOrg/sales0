"""Pipeline match + repeat rule (inbox feedback A1).

A new discovery enquiry is checked against the EXISTING pipeline before it lands in the inbox:

1. ``find_match`` - the first hit wins: author LinkedIn profile -> prospect; author profile -> known contact ->
   prospect; post email -> known contact -> prospect; author is a company page -> company (by its LinkedIn URL);
   AI-extracted company name -> company (``name_key``); company email domain -> company (``domain_key``). A company
   hit is attached to its newest live prospect when it has one. Prospects closed only because their enquiry was
   discarded do not count (they never were real pipeline).
2. ``apply`` (called by ``seen_posts.persist_triage`` for every enquiry a run creates): the enquiry records the match
   (``pipeline_*`` columns). Within ONE scrape run the first enquiry matching a prospect/company stays in To review,
   marked "Already in pipeline"; every further one is discarded by the system (``discard_source='auto'``, no
   ``triage_decisions`` row, so it never feeds the few-shot examples) with a reason, and a timeline note on the
   prospect (or company) records it so nothing is lost. Outside a run (rescue, retry, paste) it only marks.

Sheet rows are never auto-discarded: the sheet sync does not create enquiries through this path.
Pure reads + flushes; no commit, no LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Company, Contact, ContactEmail, ContactLink, Enquiry, Prospect
from sales0.services._common import clip, now_or, record_history
from sales0.services.crm import _keys, companies, timeline
from sales0.services.crm.actor import SYSTEM

MATCH_LABELS: dict[str, str] = {
    "profile": "same LinkedIn author",
    "contact": "known contact",
    "email": "known contact email",
    "company_page": "company LinkedIn page",
    "company": "same company",
    "website": "company email domain",
}
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "rediffmail.com", "zoho.com",
    "zohomail.in", "gmx.com", "mail.com", "yandex.com",
})


@dataclass(frozen=True)
class PipelineMatch:
    prospect_id: int | None
    company_id: int | None
    kind: str
    name: str

    @property
    def label(self) -> str:
        return MATCH_LABELS.get(self.kind, self.kind)


# ---------------------------------------------------------------------------- lookups
def _live(p: Prospect | None) -> bool:
    return p is not None and not (p.status == "closed" and p.closed_reason == "enquiry_discarded")


def _not_self(e: Enquiry):  # type: ignore[no-untyped-def]
    return or_(Prospect.origin_enquiry_id.is_(None), Prospect.origin_enquiry_id != e.id)


def _prospect_for_contact(db: Session, e: Enquiry, contact_id: int) -> Prospect | None:
    rows = list(db.scalars(select(Prospect).where(Prospect.contact_id == contact_id, _not_self(e)).order_by(Prospect.id.desc())))
    linked = select(ContactLink.entity_id).where(ContactLink.contact_id == contact_id, ContactLink.entity_type == "prospect")
    rows += list(db.scalars(select(Prospect).where(Prospect.id.in_(linked), _not_self(e)).order_by(Prospect.id.desc())))
    return next((p for p in rows if _live(p)), None)


def _prospect_for_company(db: Session, e: Enquiry, company_id: int) -> Prospect | None:
    rows = db.scalars(
        select(Prospect).where(Prospect.company_id == company_id, _not_self(e)).order_by(Prospect.updated_at.desc(), Prospect.id.desc())
    ).all()
    live = [p for p in rows if _live(p)]
    return next((p for p in live if p.status == "open"), live[0] if live else None)


def _contact_by_slug(db: Session, slug: str | None) -> Contact | None:
    if not slug:
        return None
    return db.scalar(select(Contact).where(Contact.linkedin_slug == slug, Contact.merged_into_id.is_(None)).limit(1))


def _contact_by_email(db: Session, email: str | None) -> Contact | None:
    value = (email or "").strip().lower()
    if "@" not in value:
        return None
    cid = db.scalar(select(ContactEmail.contact_id).where(func.lower(ContactEmail.email) == value).limit(1))
    contact = db.get(Contact, cid) if cid is not None else None
    return contact if contact is not None and contact.merged_into_id is None else None


def _company_by_domain(db: Session, email: str | None) -> Company | None:
    """The company whose website host is the e-mail's domain (the CRM's own ``domain_key`` matcher); free-mail
    providers never identify a company."""
    value = (email or "").strip().lower()
    if "@" not in value:
        return None
    domain = value.rsplit("@", 1)[1].removeprefix("www.")
    if not domain or domain in FREE_MAIL:
        return None
    return companies.by_domain(db, domain)


def _prospect_hit(p: Prospect, kind: str) -> PipelineMatch:
    return PipelineMatch(p.id, p.company_id, kind, p.name)


def _company_hit(db: Session, e: Enquiry, c: Company, kind: str) -> PipelineMatch | None:
    """The company's live prospect, else the bare company - unless its only prospects were closed because their
    enquiry was discarded (then it never really entered the pipeline)."""
    p = _prospect_for_company(db, e, c.id)
    if p is None and db.scalar(select(func.count(Prospect.id)).where(Prospect.company_id == c.id, _not_self(e))):
        return None
    return PipelineMatch(p.id if p else None, c.id, kind, p.name if p else c.name)


def find_match(db: Session, e: Enquiry) -> PipelineMatch | None:
    """The existing pipeline entity ``e`` belongs to, or None (first hit in the documented order wins)."""
    if e.author_profile_id is not None:
        p = db.scalar(select(Prospect).where(Prospect.linkedin_profile_id == e.author_profile_id, _not_self(e)))
        if _live(p):
            return _prospect_hit(p, "profile")  # type: ignore[arg-type]
    slug = _keys.linkedin_slug_of(e.author_profile_url)
    for kind, contact in (("contact", _contact_by_slug(db, slug)), ("email", _contact_by_email(db, e.email))):
        if contact is not None:
            p = _prospect_for_contact(db, e, contact.id)
            if p is not None:
                return _prospect_hit(p, kind)
    # Company keys are the CRM's (services.crm.companies.by_*): LinkedIn page, name, then e-mail domain.
    candidates = (
        ("company_page", companies.by_linkedin(db, e.author_profile_url)),
        ("company", companies.by_name(db, e.company)),
        ("website", _company_by_domain(db, e.email)),
    )
    for kind, company in candidates:
        hit = _company_hit(db, e, company, kind) if company is not None else None
        if hit is not None:
            return hit
    return None


# ---------------------------------------------------------------------------- the rule
def _earlier_in_run(db: Session, e: Enquiry, match: PipelineMatch, run_id: int) -> int:
    same = (
        Enquiry.pipeline_prospect_id == match.prospect_id
        if match.prospect_id is not None
        else (Enquiry.pipeline_company_id == match.company_id) & Enquiry.pipeline_prospect_id.is_(None)
    )
    return int(
        db.scalar(select(func.count(Enquiry.id)).where(Enquiry.pipeline_run_id == run_id, Enquiry.id != e.id, same)) or 0
    )


def repeat_reason(name: str, earlier: int) -> str:
    return clip(f"Repeat from existing prospect {name} (already {earlier} this run)", 200) or "Repeat from existing prospect"


def apply(db: Session, e: Enquiry, *, run_id: int | None, now: datetime | None = None) -> bool:
    """Record the pipeline match of a NEW enquiry; inside a run, auto-discard repeats. True = auto-discarded."""
    match = find_match(db, e)
    if match is None:
        return False
    stamp = now_or(now)
    e.pipeline_prospect_id, e.pipeline_company_id, e.pipeline_match = match.prospect_id, match.company_id, match.kind
    e.pipeline_run_id = run_id
    db.flush()
    if run_id is None or e.review_status != "pending_review":
        return False
    earlier = _earlier_in_run(db, e, match, run_id)
    if earlier < 1:
        return False
    reason = repeat_reason(match.name, earlier)
    record_history(db, "enquiry", e.id, "review_status", e.review_status, "discarded", note="auto: repeat from existing prospect", now=stamp)
    e.review_status, e.stage, e.discard_reason, e.discard_source, e.decided_at = "discarded", None, reason, "auto", stamp
    subject = ("prospect", match.prospect_id) if match.prospect_id is not None else ("company", match.company_id)
    body = f"Repeat enquiry auto-skipped ({MATCH_LABELS.get(match.kind, match.kind)})" + (
        f": {e.requirement_summary}" if e.requirement_summary else "."
    )
    timeline.add_event(
        db, subject_type=subject[0], subject_id=int(subject[1]), kind="note", actor=SYSTEM, occurred_at=stamp,
        body=clip(body, 500), enquiry_id=e.id,
        data={"system": True, "auto_discard": "repeat", "enquiry_id": e.id, "run_id": run_id, "match": match.kind,
              "reason": reason},
    )
    db.flush()
    return True


def view_for(db: Session, e: Enquiry) -> tuple[str, str] | None:
    """(display name, href) of the pipeline entity an enquiry matched, read live (None when gone / no match)."""
    if e.pipeline_prospect_id is not None:
        p = db.get(Prospect, e.pipeline_prospect_id)
        if p is not None:
            return p.name, f"/pipeline/prospects/{p.id}"
    if e.pipeline_company_id is not None:
        c = db.get(Company, e.pipeline_company_id)
        if c is not None:
            return c.name, f"/companies/{c.id}"
    return None


def prospect_for_accept(db: Session, e: Enquiry) -> Prospect | None:
    """The existing prospect an accepted enquiry joins: the one it was MARKED with when it was created ("Already in
    pipeline"), if that prospect still counts. Unmarked enquiries (older ones, sheet rows) keep the CRM's own identity
    rules (one prospect per LinkedIn profile, else per enquiry)."""
    if e.pipeline_prospect_id is None or e.origin == "sheet":
        return None
    p = db.get(Prospect, e.pipeline_prospect_id)
    return p if _live(p) else None


__all__ = ["MATCH_LABELS", "PipelineMatch", "apply", "find_match", "prospect_for_accept", "repeat_reason", "view_for"]

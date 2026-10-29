"""The AI account summary of a prospect: one Haiku call on "Refresh summary", stored as a version; human edits
are versions too (``prospect_docs`` doc_type ``summary``). Drafting reads the newest usable version."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from sales0.agent.summary import summarise_account
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMClient
from sales0.logging import get_logger
from sales0.services._common import now_or
from sales0.services.costs_report import ensure_within_spend_cap
from sales0.services.crm.actor import Actor
from sales0.services.research import context, docs, linkedin_page
from sales0.services.research.docs import DocView, SourceRef

log = get_logger("sales0.research.summary")

MAX_SUMMARY_CHARS = 6000


def refresh_summary(
    db: Session, llm: LLMClient | None, member: TeamMember | None, prospect_id: int, *, actor: Actor, now: datetime | None = None
) -> DocView:
    """One AI call; a new version (also when it failed, so the page says what happened). Flushed, caller commits."""
    stamp = now_or(now)
    ensure_within_spend_cap(db, stamp)
    b = context.basics(db, prospect_id)
    page = linkedin_page.latest_text(db, prospect_id)
    records = context.build(db, prospect_id, detail="summary", linkedin_text=page.result_text if page is not None else None)
    result = summarise_account(db, llm, name=b.name + (f" ({b.company_name})" if b.company_name and b.company_name != b.name else ""),
                               records=records)
    research = docs.latest_usable(db, prospect_id, "research")
    sources = [SourceRef(kind="own_data", label="Our records (enquiries, contacts, notes, emails, replies, follow-ups, sheet)")]
    if research is not None:
        sources.append(SourceRef(kind="own_data", label=f"Research version {research.version}"))
    if page is not None:
        sources.append(SourceRef(kind="linkedin_page", label="LinkedIn company page", url=page.result_url or page.target_url))
    version = docs.add_version(
        db, prospect_id, "summary", origin="ai", actor=actor, status=result.status, error=result.error,
        body=result.text or None, data=result.summary.model_dump() if result.summary else None, sources=sources,
        inputs={"records_chars": len(records), "research_version": research.version if research else None,
                "linkedin_page": bool(page)},
        cost_usd_micros=result.cost_usd_micros, now=stamp,
    )  # fmt: skip
    log.info("account_summary_refreshed", prospect_id=prospect_id, version=version.version, status=result.status,
             cost=result.cost_usd_micros, member_id=member.id if member else None)
    return version


def edit_summary(db: Session, prospect_id: int, body: str, *, actor: Actor, now: datetime | None = None) -> DocView:
    text = (body or "").replace("\r\n", "\n").strip()
    if not text:
        raise DomainError("summary_invalid", "The summary cannot be empty.", 400)
    if len(text) > MAX_SUMMARY_CHARS:
        raise DomainError("summary_invalid", f"The summary is at most {MAX_SUMMARY_CHARS} characters.", 400)
    current = docs.latest(db, prospect_id, "summary")
    return docs.add_version(
        db, prospect_id, "summary", origin="human", actor=actor, status="done", body=text,
        sources=current.sources if current else [], based_on_version=current.version if current else None,
        inputs={"edited_from": current.version if current else None}, now=now,
    )  # fmt: skip


def summary_for_drafting(db: Session, prospect_id: int | None) -> str | None:
    if prospect_id is None:
        return None
    doc = docs.latest_usable(db, prospect_id, "summary")
    return doc.body if doc is not None and doc.body else None


__all__ = ["edit_summary", "refresh_summary", "summary_for_drafting"]

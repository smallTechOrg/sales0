"""Pasted-post import: one post typed or pasted by hand -> dedupe -> enquiry in To review -> ONE triage call.

Idempotent: the same text (same normalised content hash), the same post link, or the same
author's near-identical opening text answers ``duplicate`` and creates nothing. The pasted
enquiry is app-owned (``origin = manual``) and goes through the normal inbox actions; accepting
it creates the prospect (``crm.hooks.on_enquiry_decided``), so no prospect is made here.
A ``seen_posts`` row is kept so a later discovery sighting of the same post dedupes against it.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, EnquirySource, SeenPost
from sales0.errors import DomainError
from sales0.llm import LLMClient
from sales0.logging import get_logger
from sales0.scraper.types import ScrapedPost
from sales0.scraper.urls import link_kind_of, profile_slug, urn_from_href
from sales0.services import profiles
from sales0.services._common import clip, now_or, record_history
from sales0.services.crm import timeline
from sales0.services.crm.actor import Actor
from sales0.services.dedupe import _hash_for, _near_duplicate_of, normalize_text
from sales0.services.importer.batches import record_batch
from sales0.services.importer.views import PasteResult
from sales0.services.seen_posts import _text_prefix, fingerprint, snippet_of
from sales0.services.triage import FATAL_LLM_ERRORS, triage_enquiry

log = get_logger("sales0.importer.paste")

MIN_CHARS = 40
MIN_WORD_CHARS = 10  # after links and hashtags are stripped: a post of only links is not a post
MAX_TEXT_CHARS = 20000
INVALID_TEXT = "Paste at least 40 characters of the post."
INVALID_LINK = "That link does not look like a web address (start it with https://)."


def _clean_link(link: str | None) -> str | None:
    value = (link or "").strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise DomainError("paste_invalid", INVALID_LINK, 400)
    return value[:1000]


def _existing(db: Session, *, ref: str, chash: str, fp: str, link: str | None, post: ScrapedPost) -> tuple[int | None, SeenPost | None]:  # noqa: E501
    """(enquiry_id of a duplicate | None, a seen_posts row without an enquiry that may be adopted | None)."""
    hit = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref == ref))
    if hit is None:
        hit = db.scalar(select(Enquiry.id).where(Enquiry.content_hash == chash).order_by(Enquiry.id).limit(1))
    if hit is None and link and link_kind_of(link) == "post":
        hit = db.scalar(select(Enquiry.id).where(Enquiry.source_url == link).order_by(Enquiry.id).limit(1))
    if hit is None:
        hit = _near_duplicate_of(db, post)
    seen = db.scalar(
        select(SeenPost)
        .where((SeenPost.source_ref == ref) | (SeenPost.fingerprint == fp) | (SeenPost.content_hash == chash))
        .order_by(SeenPost.id)
        .limit(1)
    )
    if hit is None and seen is not None and seen.enquiry_id is not None:
        hit = seen.enquiry_id
    return (int(hit) if hit is not None else None), seen


def paste_post(
    db: Session,
    llm: LLMClient | None,
    *,
    text: str,
    link: str | None = None,
    author: str | None = None,
    author_profile_url: str | None = None,
    actor: Actor,
    now: datetime | None = None,
) -> PasteResult:
    """Add one pasted post to To review. Flushes, never commits. ``paste_invalid`` 400 under 40 chars."""
    stamp = now_or(now)
    body = (text or "").replace("\r\n", "\n").strip()
    if len(body) < MIN_CHARS or sum(ch.isalnum() for ch in normalize_text(body)) < MIN_WORD_CHARS:
        raise DomainError("paste_invalid", INVALID_TEXT, 400)
    body = body[:MAX_TEXT_CHARS]
    url = _clean_link(link)
    profile_url = _clean_link(author_profile_url)
    name = clip(author, 200)

    post_link = url if url and link_kind_of(url) == "post" else None
    urn = urn_from_href(post_link) if post_link else None
    probe = ScrapedPost(
        source_ref=urn or "paste:pending", url=url or "", author_name=name, author_profile_url=profile_url,
        author_headline=None, posted_relative=None, posted_date=None, text=body,
        link_kind="post" if post_link else "other",
    )  # fmt: skip
    chash = _hash_for(probe)
    ref = urn or f"paste:{chash[:40]}"
    slug = profile_slug(profile_url)
    fp = fingerprint(slug, body)

    enquiry_id, seen = _existing(db, ref=ref, chash=chash, fp=fp, link=post_link, post=probe)
    if enquiry_id is not None:
        return PasteResult(enquiry_id=enquiry_id, outcome="duplicate")

    profile_id = profiles.touch_profile(db, profile_url=profile_url, name=name, headline=None, now=stamp)
    enquiry = Enquiry(
        source="linkedin_post", content_hash=chash, raw_text=body, author_name=name,
        author_profile_url=profile_url, source_url=url, date_added=stamp, review_status="pending_review",
        triage_status="pending", author_profile_id=profile_id, origin="manual",
    )  # fmt: skip
    db.add(enquiry)
    db.flush()
    db.add(EnquirySource(enquiry_id=enquiry.id, source_ref=ref, source_url=url, first_seen_at=stamp))
    if seen is None:
        seen = SeenPost(
            fingerprint=fp, content_hash=chash, text_prefix=_text_prefix(body), source_ref=ref, profile_id=profile_id,
            author_name=name, keyword_text="", triage_status="pending", snippet=snippet_of(body),
            post_url=post_link[:500] if post_link else None, first_seen_at=stamp, last_seen_at=stamp,
        )  # fmt: skip
        db.add(seen)
    else:  # an earlier sighting the AI skipped: the human decided it is worth a look
        seen.times_seen = (seen.times_seen or 0) + 1
        seen.last_seen_at = stamp
        seen.rescued_at = stamp
        seen.raw_text = None
        seen.raw_purge_after = None
        if seen.profile_id is None:
            seen.profile_id = profile_id
    seen.enquiry_id = enquiry.id
    db.flush()
    enquiry.seen_post_id = seen.id
    record_history(db, "enquiry", enquiry.id, "review_status", None, "pending_review", note=f"pasted by {actor.label}", now=stamp)  # noqa: E501
    timeline.add_event(
        db, subject_type="enquiry", subject_id=enquiry.id, kind="created", body="Pasted by hand", actor=actor,
        occurred_at=stamp, enquiry_id=enquiry.id,
    )  # fmt: skip
    batch = record_batch(db, filename="pasted post", kind="paste", actor=actor, counts=(1, 1, 0, 0, 0), issues=[], now=stamp)  # noqa: E501

    # ONE real triage call. A refused/failed call keeps the enquiry (triage_status=failed, Retry in the inbox).
    try:
        triage_enquiry(db, enquiry.id, llm, now=stamp)
    except FATAL_LLM_ERRORS as exc:
        enquiry.triage_status = "failed"
        enquiry.triage_error = clip(getattr(exc, "message", None) or str(exc) or type(exc).__name__, 300)
        log.warning("paste_triage_refused", error=type(exc).__name__)
    db.flush()
    seen.triage_status = enquiry.triage_status
    seen.triage_error = enquiry.triage_error
    seen.verdict = enquiry.ai_verdict
    seen.reason = enquiry.ai_reason
    seen.poster_type = enquiry.ai_poster_type
    seen.ai_result = enquiry.ai_extraction
    db.flush()
    return PasteResult(
        enquiry_id=enquiry.id, outcome="created", verdict=enquiry.ai_verdict, triage_status=enquiry.triage_status,
        batch_id=batch.id,
    )  # fmt: skip


__all__ = ["INVALID_TEXT", "MIN_CHARS", "paste_post"]

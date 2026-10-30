"""Seen posts (Phase 1.4): every scanned post is remembered once, triaged once, stored as an
enquiry only when the AI calls it genuine (or a human rescues it).

Match order for a sighting: source ref -> fingerprint -> content hash -> same-profile text prefix.
All functions take ``db`` first, flush, never commit (``retry_triage`` is the one exception, like
``inbox.retry_triage``), and take the clock via ``now``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from sales0.agent import TriageInput, TriageOutcome, TriageResult
from sales0.agent.nodes import PROMPT_VERSION
from sales0.config import get_settings
from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    EnquirySource,
    Keyword,
    LinkedinProfile,
    LlmCall,
    Offering,
    SeenPost,
)
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMClient, LLMModelNotFound
from sales0.logging import get_logger
from sales0.scraper.relative_time import parse_relative
from sales0.scraper.types import ScrapedPost
from sales0.scraper.urls import base_url, link_kind_of, profile_slug, search_url
from sales0.services import profiles
from sales0.services._common import clip, not_found, now_or, record_history
from sales0.services.dedupe import (
    FINGERPRINT_CHARS,
    FINGERPRINT_MIN_OVERLAP,
    _add_hit,
    _hash_for,
    _merge,
    normalize_text,
    strip_ellipsis,
)
from sales0.services.views import EnquiryCardView, SkippedPage, SkippedPostView

log = get_logger("sales0.services.seen_posts")

Verdict = Literal["genuine", "discard"]
MAX_PAGE_SIZE = 100
SNIPPET_CHARS = 240


# ---------------------------------------------------------------------- pure helpers
def fingerprint(author_slug: str | None, text: str) -> str:
    """``sha256((author_slug or "") + "\\n" + normalize_text(text))``."""
    return hashlib.sha256(f"{author_slug or ''}\n{normalize_text(text)}".encode()).hexdigest()


def snippet_of(text: str) -> str:
    """One line, at most 240 characters (an ellipsis marks a cut)."""
    line = " ".join((text or "").split())
    if len(line) <= SNIPPET_CHARS:
        return line
    return line[: SNIPPET_CHARS - 1].rstrip() + "…"


def _text_prefix(text: str) -> str:
    return normalize_text(strip_ellipsis(text))[:FINGERPRINT_CHARS]


def _post_fingerprint(author_slug: str | None, post: ScrapedPost) -> str:
    # Text that normalises to nothing must not collapse every empty post of one author.
    if not normalize_text(post.text):
        return hashlib.sha256(f"empty:{post.source_ref}".encode()).hexdigest()
    return fingerprint(author_slug, post.text)


# ---------------------------------------------------------------------- sighting
@dataclass(frozen=True)
class Sighting:
    kind: Literal["new", "seen"]
    seen_post_id: int
    profile_id: int | None
    verdict: Verdict | None
    triage_status: Literal["pending", "done", "failed"]
    enquiry_id: int | None
    needs_permalink: bool  # seen + has an enquiry whose source_url is not a post link


def _by_source_ref(db: Session, post: ScrapedPost) -> SeenPost | None:
    refs = [r for r in dict.fromkeys((post.source_ref, post.urn)) if r]
    if not refs:
        return None
    row = db.scalar(select(SeenPost).where(SeenPost.source_ref.in_(refs)).order_by(SeenPost.id).limit(1))
    if row is not None:
        return row
    # Refs recorded only as enquiry sources (e.g. a URN learned from "Copy link to post").
    enquiry_id = db.scalar(select(EnquirySource.enquiry_id).where(EnquirySource.source_ref.in_(refs)).limit(1))
    return _seen_for_enquiry(db, enquiry_id) if enquiry_id is not None else None


def _seen_for_enquiry(db: Session, enquiry_id: int) -> SeenPost | None:
    row = db.scalar(select(SeenPost).where(SeenPost.enquiry_id == enquiry_id).limit(1))
    if row is not None:
        return row
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None:
        return None
    # An enquiry without a seen_posts row (created before 0003 and missed): adopt it.
    slug = profile_slug(enquiry.author_profile_url)
    row = SeenPost(
        fingerprint=fingerprint(slug, enquiry.raw_text),
        content_hash=enquiry.content_hash,
        text_prefix=_text_prefix(enquiry.raw_text),
        profile_id=enquiry.author_profile_id,
        author_name=enquiry.author_name,
        author_headline=enquiry.author_headline,
        keyword_text="",
        triage_status=enquiry.triage_status,
        verdict=enquiry.ai_verdict,
        reason=enquiry.ai_reason,
        poster_type=enquiry.ai_poster_type,
        snippet=snippet_of(enquiry.raw_text),
        post_url=enquiry.source_url if link_kind_of(enquiry.source_url) == "post" else None,
        enquiry_id=enquiry.id,
        first_seen_at=enquiry.date_added,
        last_seen_at=enquiry.date_added,
    )
    db.add(row)
    db.flush()
    enquiry.seen_post_id = row.id
    return row


def _near_duplicate(db: Session, profile_id: int | None, prefix: str) -> SeenPost | None:
    if profile_id is None or len(prefix) < FINGERPRINT_MIN_OVERLAP:
        return None
    for row in db.scalars(select(SeenPost).where(SeenPost.profile_id == profile_id).order_by(SeenPost.id)):
        n = min(len(row.text_prefix), len(prefix))
        if n >= FINGERPRINT_MIN_OVERLAP and row.text_prefix[:n] == prefix[:n]:
            return row
    return None


def _needs_permalink(db: Session, row: SeenPost) -> bool:
    if row.enquiry_id is None:
        return False
    url = db.scalar(select(Enquiry.source_url).where(Enquiry.id == row.enquiry_id))
    return link_kind_of(url) != "post"


def _collapse(text: str) -> str:
    return " ".join(strip_ellipsis(text or "").split())


def _refresh_from_resighting(db: Session, row: SeenPost, post: ScrapedPost, slug: str | None, stamp: datetime) -> None:
    """Fill what the first sighting missed (post time, headline, name) and upgrade truncated text.

    Existing values are never overwritten. A longer text that starts with the stored one replaces it
    (fingerprint/prefix/snippet refreshed so the full text from the same author matches directly next
    time; content_hash is kept so another author's identical text is still its own post); a row
    the AI discarded or failed on is re-queued as pending so the full text is triaged. A failed row
    seen again with the same text is re-queued too (retry)."""
    if not row.posted_relative and post.posted_relative:
        row.posted_relative = clip(post.posted_relative, 30)
    if row.posted_date is None and post.posted_date is not None:
        row.posted_date = post.posted_date
    if row.link_kind is None:
        row.link_kind = clip(post.link_kind, 10)
    if row.search_url is None and post.search_url:
        row.search_url = clip(post.search_url, 2000)
    if row.raw_card is None and post.raw_card:
        row.raw_card = post.raw_card
    if post.truncated is False and row.truncated:  # a later sighting expanded the text fully
        row.truncated = False
    elif row.truncated is None:
        row.truncated = post.truncated
    if not row.author_headline and post.author_headline:
        row.author_headline = clip(post.author_headline, 500)
    if not row.author_name and post.author_name:
        row.author_name = clip(post.author_name, 200)
    if row.enquiry_id is not None:
        enquiry = db.get(Enquiry, row.enquiry_id)
        if enquiry is not None:
            if not enquiry.posted_relative and row.posted_relative:
                enquiry.posted_relative = row.posted_relative
            if not enquiry.author_headline and row.author_headline:
                enquiry.author_headline = row.author_headline
        return
    if not row.raw_text or not normalize_text(post.text):
        return
    old, new = _collapse(row.raw_text), _collapse(post.text)
    upgraded = len(old) >= 20 and len(new) > len(old) and new.startswith(old)
    if upgraded:
        row.raw_text = post.text
        row.text_prefix = _text_prefix(post.text)
        row.snippet = snippet_of(post.text)
        fp = _post_fingerprint(slug, post)
        if row.fingerprint != fp and db.scalar(select(SeenPost.id).where(SeenPost.fingerprint == fp)) is None:
            row.fingerprint = fp
    requeue = row.triage_status == "failed" or (
        upgraded and row.triage_status == "done" and row.verdict == "discard" and row.rescued_at is None
    )
    if requeue:
        row.triage_status = "pending"
        row.triage_error = None
        row.verdict = None
        row.reason = None
        row.poster_type = None
        row.ai_result = None
        row.raw_purge_after = raw_purge_after(stamp)
    if upgraded or requeue:
        log.info("seen_post_resighting_refresh", seen_post_id=row.id, text_upgraded=upgraded, requeued=requeue)


def raw_purge_after(stamp: datetime) -> datetime | None:
    """When a seen post's raw text may be purged: never (None) unless SALES0_SEEN_RAW_RETENTION_DAYS > 0."""
    days = get_settings().seen_raw_retention_days
    return stamp + timedelta(days=days) if days > 0 else None


def register_sighting(
    db: Session,
    post: ScrapedPost,
    run_id: int,
    keyword_id: int | None,
    keyword_text: str,
    *,
    now: datetime | None = None,
) -> Sighting:
    """Record that ``post`` was scanned. Never calls the LLM."""
    stamp = now_or(now)
    profile_id = profiles.touch_profile(
        db,
        profile_url=post.author_profile_url,
        name=post.author_name,
        headline=post.author_headline,
        now=stamp,
    )
    slug = profile_slug(post.author_profile_url)
    fp = _post_fingerprint(slug, post)
    prefix = _text_prefix(post.text)

    row = _by_source_ref(db, post)
    if row is None:
        row = db.scalar(select(SeenPost).where(SeenPost.fingerprint == fp))
    if row is None:
        row = db.scalar(
            select(SeenPost).where(SeenPost.content_hash == _hash_for(post)).order_by(SeenPost.id).limit(1)
        )
    if row is None:
        enquiry_id = db.scalar(select(Enquiry.id).where(Enquiry.content_hash == _hash_for(post)))
        if enquiry_id is not None:
            row = _seen_for_enquiry(db, enquiry_id)
    if row is None:
        row = _near_duplicate(db, profile_id, prefix)

    if row is not None:
        row.times_seen = (row.times_seen or 0) + 1
        row.last_seen_at = stamp
        if row.profile_id is None and profile_id is not None:
            row.profile_id = profile_id
        if row.source_ref is None and post.source_ref:
            taken = db.scalar(select(SeenPost.id).where(SeenPost.source_ref == post.source_ref))
            if taken is None:
                row.source_ref = post.source_ref
        if post.link_kind == "post" and row.post_url is None:
            row.post_url = post.url[:500]
        _refresh_from_resighting(db, row, post, slug, stamp)
        if row.enquiry_id is not None:
            _merge(db, row.enquiry_id, post, run_id, stamp)
            _add_hit(db, row.enquiry_id, run_id, keyword_id, keyword_text, stamp)
        db.flush()
        return Sighting(
            kind="seen",
            seen_post_id=row.id,
            profile_id=row.profile_id,
            verdict=row.verdict,  # type: ignore[arg-type]
            triage_status=row.triage_status,  # type: ignore[arg-type]
            enquiry_id=row.enquiry_id,
            needs_permalink=_needs_permalink(db, row),
        )

    row = SeenPost(
        fingerprint=fp,
        content_hash=_hash_for(post),
        text_prefix=prefix,
        source_ref=post.source_ref or None,
        profile_id=profile_id,
        author_name=clip(post.author_name, 200),
        author_headline=clip(post.author_headline, 500),
        keyword_text=(clip(keyword_text, 100) or ""),
        first_run_id=run_id,
        triage_status="pending",
        snippet=snippet_of(post.text),
        raw_text=post.text,
        raw_purge_after=raw_purge_after(stamp),
        post_url=post.url[:500] if post.link_kind == "post" else None,
        posted_relative=clip(post.posted_relative, 30),
        posted_date=post.posted_date,
        truncated=post.truncated,
        link_kind=clip(post.link_kind, 10),
        search_url=clip(post.search_url, 2000),
        raw_card=post.raw_card,
        first_seen_at=stamp,
        last_seen_at=stamp,
        times_seen=1,
    )
    db.add(row)
    db.flush()
    return Sighting("new", row.id, profile_id, None, "pending", None, False)


# ---------------------------------------------------------------------- triage
def _profile_url(db: Session, profile_id: int | None) -> str | None:
    if profile_id is None:
        return None
    return db.scalar(select(LinkedinProfile.profile_url).where(LinkedinProfile.id == profile_id))


def _estimated_date(row: SeenPost) -> date | None:
    return parse_relative(row.posted_relative, row.first_seen_at.date())


def _input_for(db: Session, row: SeenPost) -> TriageInput:
    return TriageInput(
        subject="seen_post",  # type: ignore[call-arg]
        enquiry_id=row.id,
        text=row.raw_text or "",
        author_name=row.author_name,
        author_headline=row.author_headline,
        source_url=row.post_url or _profile_url(db, row.profile_id),
        date_posted=_estimated_date(row),
    )


def triage_inputs(db: Session, seen_post_ids: list[int]) -> list[tuple[int, TriageInput]]:
    """Triage inputs for the given seen posts (those whose text is still held), in the given order."""
    ids = list(dict.fromkeys(int(i) for i in seen_post_ids))
    if not ids:
        return []
    rows = {r.id: r for r in db.scalars(select(SeenPost).where(SeenPost.id.in_(ids))).all()}
    return [(i, _input_for(db, rows[i])) for i in ids if i in rows and rows[i].raw_text]


@dataclass(frozen=True)
class Persisted:
    seen_post_id: int
    verdict: Verdict | None
    enquiry_id: int | None
    cost_usd_micros: int
    triage_status: str
    repeat_skipped: bool = False  # q14: the new enquiry was auto-discarded as a repeat from an existing prospect


def _offering_or_none(db: Session, offering_id: int | None) -> int | None:
    if offering_id is None:
        return None
    return offering_id if db.get(Offering, offering_id) is not None else None


def _keyword_offering(db: Session, keyword_id: int | None) -> int | None:
    """The active offering a keyword is tied to (the fallback when the AI picks none)."""
    if keyword_id is None:
        return None
    return db.scalar(
        select(Offering.id)
        .join(Keyword, Keyword.offering_id == Offering.id)
        .where(Keyword.id == keyword_id, Offering.active.is_(True))
    )


def _search_url_for(row: SeenPost) -> str | None:
    if not row.keyword_text:
        return None
    settings = get_settings()
    try:
        return search_url(base_url(settings), row.keyword_text, settings.scrape_date_filter)
    except Exception:  # noqa: BLE001 - a link fallback must never break persistence
        return None


def _create_enquiry(
    db: Session,
    row: SeenPost,
    result: TriageResult,
    *,
    run_id: int | None,
    keyword_id: int | None,
    prompt_version: str,
    triaged_at: datetime,
    now: datetime,
) -> Enquiry:
    """The enquiry of a genuine (or rescued) seen post; the text moves from seen_posts to the enquiry."""
    assert row.raw_text is not None
    profile_url = _profile_url(db, row.profile_id)
    searched = _search_url_for(row)
    offering_id = _offering_or_none(db, result.offering_id)
    enquiry = Enquiry(
        source="linkedin_post",
        content_hash=row.content_hash,
        raw_text=row.raw_text,
        author_name=row.author_name,
        author_headline=row.author_headline,
        author_profile_url=profile_url,
        source_url=row.post_url or profile_url or searched,
        date_posted=_estimated_date(row),
        date_added=now,
        review_status="pending_review",
        triage_status="done",
        triaged_at=triaged_at,
        prompt_version=prompt_version,
        ai_verdict=row.verdict,
        ai_reason=row.reason or clip(result.reason, 160) or "No reason given",
        ai_poster_type=row.poster_type or result.poster_type,
        ai_opportunity_type=result.opportunity_type,
        ai_priority=result.priority,
        ai_category=result.category,
        category=result.category,
        ai_offering_id=offering_id,
        ai_extraction=result.model_dump(mode="json"),
        requirement_summary=clip(result.requirement_summary, 140),
        priority=result.priority,
        offering_id=offering_id if offering_id is not None else _keyword_offering(db, keyword_id),
        company=clip(result.company, 200),
        contact_person=clip(result.contact_person, 200),
        email=clip(result.email, 254),
        phone=clip(result.phone, 50),
        city=clip(result.city, 100),
        mode=result.mode,
        dates_mentioned=clip(result.dates_mentioned, 300),
        topic=clip(result.topic, 300),
        author_profile_id=row.profile_id,
        seen_post_id=row.id,
        posted_relative=row.posted_relative,
    )
    db.add(enquiry)
    db.flush()
    if row.source_ref:
        if db.scalar(select(EnquirySource.id).where(EnquirySource.source_ref == row.source_ref)) is None:
            db.add(
                EnquirySource(
                    enquiry_id=enquiry.id,
                    source_ref=row.source_ref,
                    # post permalink -> author profile -> (only if neither) the search page (inbox feedback A2)
                    source_url=row.post_url or profile_url or searched,
                    first_seen_run_id=row.first_run_id,
                    first_seen_at=row.first_seen_at,
                )
            )
    if row.keyword_text:
        db.add(
            EnquiryKeywordHit(
                enquiry_id=enquiry.id,
                run_id=run_id if run_id is not None else row.first_run_id,
                keyword_id=keyword_id,
                keyword_text=row.keyword_text,
                seen_at=now,
            )
        )
    record_history(db, "enquiry", enquiry.id, "triage_status", None, "done", note="created from a seen post", now=now)
    record_history(db, "enquiry", enquiry.id, "review_status", None, "pending_review", now=now)
    row.enquiry_id = enquiry.id
    row.raw_text = None
    row.raw_purge_after = None
    db.execute(update(LlmCall).where(LlmCall.seen_post_id == row.id).values(enquiry_id=enquiry.id))
    db.flush()
    profiles.mark_relevant(db, row.profile_id)
    _pipeline_check(db, enquiry, run_id=run_id, now=now)
    return enquiry


def _pipeline_check(db: Session, enquiry: Enquiry, *, run_id: int | None, now: datetime) -> None:
    """Mark a match with the existing pipeline; inside a run, auto-discard repeats (services.crm.pipeline_match).
    A failure here must never lose the enquiry: it is logged and the enquiry stays in To review."""
    from sales0.services.crm import pipeline_match

    try:
        with db.begin_nested():
            pipeline_match.apply(db, enquiry, run_id=run_id, now=now)
    except Exception as exc:  # noqa: BLE001
        log.exception("pipeline_check_failed", enquiry_id=enquiry.id, error_type=type(exc).__name__)


def persist_triage(
    db: Session,
    seen_post_id: int,
    outcome: TriageOutcome | None,
    *,
    error: str | None = None,
    run_id: int | None,
    keyword_id: int | None,
    now: datetime | None = None,
) -> Persisted:
    """Store one triage outcome. A genuine verdict creates the enquiry; a discard stays here."""
    stamp = now_or(now)
    row = db.get(SeenPost, seen_post_id)
    if row is None:
        raise not_found()
    settings = get_settings()
    cost = 0
    version = outcome.prompt_version if outcome is not None else PROMPT_VERSION
    if outcome is not None:
        for resp in outcome.llm_responses:
            cost += resp.cost_usd_micros
            db.add(
                LlmCall(
                    feature="triage",
                    model=resp.model[:100],
                    prompt_version=outcome.prompt_version,
                    seen_post_id=row.id,
                    run_id=run_id,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    cost_usd_micros=resp.cost_usd_micros,
                    latency_ms=resp.latency_ms,
                    fewshot_count=outcome.fewshot_count,
                    status="ok",
                    request_id=resp.request_id[:100] if resp.request_id else None,
                    criteria_version=outcome.criteria_version,
                    created_at=stamp,
                )
            )
    failure = clip(error or (outcome.error if outcome is not None else None) or "AI triage failed", 300)
    if outcome is None or outcome.status != "done" or outcome.result is None:
        if outcome is None or not outcome.llm_responses:
            db.add(
                LlmCall(
                    feature="triage",
                    model=settings.llm_model,
                    prompt_version=version,
                    seen_post_id=row.id,
                    run_id=run_id,
                    fewshot_count=outcome.fewshot_count if outcome is not None else 0,
                    status="error",
                    error_type=clip(failure, 100),
                    created_at=stamp,
                )
            )
        row.triage_status = "failed"
        row.triage_error = failure
        db.flush()
        return Persisted(row.id, row.verdict, row.enquiry_id, cost, "failed")  # type: ignore[arg-type]

    result = outcome.result
    row.triage_status = "done"
    row.triage_error = None
    row.verdict = result.verdict
    row.reason = clip(result.reason, 160) or "No reason given"
    row.poster_type = clip(result.poster_type, 40)
    row.ai_result = result.model_dump(mode="json")
    db.flush()
    repeat = False
    if result.verdict == "genuine" and row.enquiry_id is None and row.raw_text:
        enquiry = _create_enquiry(
            db, row, result, run_id=run_id, keyword_id=keyword_id, prompt_version=outcome.prompt_version,
            triaged_at=stamp, now=stamp,
        )
        repeat = enquiry.discard_source == "auto"
    return Persisted(row.id, row.verdict, row.enquiry_id, cost, "done", repeat)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- permalinks
def attach_permalink(
    db: Session, seen_post_id: int, url: str, *, urn: str | None = None, now: datetime | None = None
) -> bool:
    """Record a captured permalink. True when anything changed."""
    stamp = now_or(now)
    row = db.get(SeenPost, seen_post_id)
    if row is None:
        raise not_found()
    changed = False
    if link_kind_of(url) == "post" and row.post_url != url[:500]:
        row.post_url = url[:500]
        changed = True
    if row.enquiry_id is not None:
        enquiry = db.get(Enquiry, row.enquiry_id)
        if enquiry is not None:
            if link_kind_of(enquiry.source_url) != "post" and link_kind_of(url) == "post":
                enquiry.source_url = url
                changed = True
            if urn and db.scalar(select(EnquirySource.id).where(EnquirySource.source_ref == urn)) is None:
                db.add(
                    EnquirySource(
                        enquiry_id=enquiry.id,
                        source_ref=urn,
                        source_url=url,
                        first_seen_run_id=row.first_run_id,
                        first_seen_at=stamp,
                    )
                )
                changed = True
    db.flush()
    return changed


# ---------------------------------------------------------------------- skipped list
def _can_rescue(row: SeenPost) -> bool:
    return row.raw_text is not None and row.enquiry_id is None and row.verdict == "discard" and row.ai_result is not None


def _skipped_view(row: SeenPost, profile_url: str | None) -> SkippedPostView:
    return SkippedPostView(
        id=row.id,
        snippet=row.snippet,
        reason=row.reason,
        poster_type=row.poster_type,
        author_name=row.author_name,
        author_headline=row.author_headline,
        profile_url=profile_url,
        keyword_text=row.keyword_text,
        post_url=row.post_url,
        link_kind=link_kind_of(row.post_url or profile_url),
        posted_relative=row.posted_relative,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        times_seen=row.times_seen,
        triage_status=row.triage_status,  # type: ignore[arg-type]
        triage_error=row.triage_error,
        raw_text=row.raw_text,
        can_rescue=_can_rescue(row),
    )


def _view_of(db: Session, row: SeenPost) -> SkippedPostView:
    return _skipped_view(row, _profile_url(db, row.profile_id))


def _state_condition(state: str) -> Any:
    if state == "failed":
        return SeenPost.triage_status == "failed"
    return (SeenPost.verdict == "discard") & SeenPost.enquiry_id.is_(None)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def skipped_counts(db: Session) -> dict[str, int]:
    discard = db.scalar(select(func.count(SeenPost.id)).where(_state_condition("discard")))
    failed = db.scalar(select(func.count(SeenPost.id)).where(_state_condition("failed")))
    return {"discard": int(discard or 0), "failed": int(failed or 0)}


def list_skipped(
    db: Session,
    *,
    q: str | None = None,
    state: Literal["discard", "failed"] = "discard",
    page: int = 1,
    page_size: int = 25,
) -> SkippedPage:
    state = "failed" if state == "failed" else "discard"
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or 25), 1), MAX_PAGE_SIZE)
    query = (q or "").strip() or None
    conditions: list[Any] = [_state_condition(state)]
    if query:
        pattern = _like(query)
        conditions.append(
            or_(
                SeenPost.snippet.ilike(pattern, escape="\\"),
                SeenPost.reason.ilike(pattern, escape="\\"),
                SeenPost.author_name.ilike(pattern, escape="\\"),
                SeenPost.raw_text.ilike(pattern, escape="\\"),
            )
        )
    total = int(db.scalar(select(func.count(SeenPost.id)).where(*conditions)) or 0)
    rows = db.execute(
        select(SeenPost, LinkedinProfile.profile_url)
        .outerjoin(LinkedinProfile, LinkedinProfile.id == SeenPost.profile_id)
        .where(*conditions)
        .order_by(SeenPost.last_seen_at.desc(), SeenPost.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return SkippedPage(
        items=[_skipped_view(r, url) for r, url in rows],
        total=total,
        page=page,
        page_size=page_size,
        state=state,
        q=query,
        counts=skipped_counts(db),
    )


# ---------------------------------------------------------------------- rescue / retry
def rescue(
    db: Session, seen_post_id: int, *, accept: bool = False, actor_user_id: int | None = None,
    acted_by_member_id: int | None = None,
) -> EnquiryCardView:
    """Turn a skipped post into an enquiry in To review (the AI verdict stays "discard").
    ``accept=True`` also records an accept decision (an override)."""
    from sales0.services import inbox

    row = db.get(SeenPost, seen_post_id)
    if row is None:
        raise not_found()
    if row.enquiry_id is not None:
        raise DomainError("already_rescued", "This post is already in your inbox.", 409)
    if row.raw_text is None:
        raise DomainError(
            "raw_purged",
            "The post text is no longer stored (kept 14 days) — it can't be rescued. Find it again in a later run.",
            410,
        )
    if row.ai_result is None or row.verdict is None:
        raise DomainError("triage_failed", "This post has no AI result yet — retry triage first.", 409)
    if db.scalar(select(Enquiry.id).where(Enquiry.content_hash == row.content_hash)) is not None:
        raise DomainError("already_rescued", "This post is already in your inbox.", 409)
    stamp = now_or(None)
    result = TriageResult.model_validate(row.ai_result)
    enquiry = _create_enquiry(
        db, row, result, run_id=None, keyword_id=None, prompt_version=_prompt_version_of(db, row.id),
        triaged_at=stamp, now=stamp,
    )
    row.rescued_at = stamp
    enquiry.actor_user_id = actor_user_id
    db.flush()
    if accept:
        return inbox.decide(
            db, [enquiry.id], "accept", actor_user_id=actor_user_id, actor_member_id=actor_user_id,
            acted_by_member_id=acted_by_member_id, now=stamp,
        )[0]
    return inbox.get_card(db, enquiry.id)


def _prompt_version_of(db: Session, seen_post_id: int) -> str:
    return (
        db.scalar(
            select(LlmCall.prompt_version)
            .where(LlmCall.seen_post_id == seen_post_id, LlmCall.prompt_version.is_not(None))
            .order_by(LlmCall.id.desc())
            .limit(1)
        )
        or PROMPT_VERSION
    )


def retry_triage(db: Session, seen_post_id: int, llm: LLMClient | None = None) -> SkippedPostView:
    """One synchronous real triage call for a failed seen post. Commits the outcome, even a failure."""
    from sales0.agent import TriageDeps, load_candidates, run_triage, select_examples
    from sales0.llm import get_llm_client
    from sales0.services import criteria
    from sales0.services.triage import active_offering_refs

    row = db.get(SeenPost, seen_post_id)
    if row is None:
        raise not_found()
    if row.raw_text is None:
        raise DomainError(
            "raw_purged", "The post text is no longer stored (kept 14 days) — it can't be triaged again.", 410
        )
    if row.triage_status == "done":
        return _view_of(db, row)
    settings = get_settings()
    inp = _input_for(db, row)
    keyword_id = db.scalar(select(Keyword.id).where(Keyword.text_key == row.keyword_text.lower()))
    try:
        client = llm or get_llm_client()
        candidates = load_candidates(db, exclude_enquiry_id=0)
        examples = select_examples(candidates, settings.fewshot_max_examples, settings.fewshot_text_chars)
        outcome = run_triage(
            inp,
            TriageDeps(
                llm=client, offerings=active_offering_refs(db), examples=examples, settings=settings,
                criteria=criteria.active_set(db),
            ),
        )
    except (LLMAuthError, LLMModelNotFound) as exc:
        persist_triage(db, row.id, None, error=exc.message, run_id=None, keyword_id=keyword_id)
        db.commit()
        if isinstance(exc, LLMAuthError):
            raise DomainError("llm_auth", exc.message.rstrip(".") + ".", 502) from exc
        raise DomainError("triage_failed", f"AI triage failed: {exc.message}. Try again in a minute.", 502) from exc
    persisted = persist_triage(db, row.id, outcome, run_id=None, keyword_id=keyword_id)
    db.commit()
    if persisted.triage_status != "done":
        reason = (outcome.error or "unknown error").rstrip(".")
        raise DomainError("triage_failed", f"AI triage failed: {reason}. Try again in a minute.", 502)
    return _view_of(db, row)


# ---------------------------------------------------------------------- purge
def purge_expired(db: Session, *, now: datetime | None = None) -> int:
    """Drop the full text of triaged (``done``) posts past their retention; fingerprint, prefix and
    snippet stay. Pending/failed rows keep their text (they can still be triaged/retried)."""
    stamp = now_or(now)
    result = db.execute(
        update(SeenPost)
        .where(
            SeenPost.raw_purge_after <= stamp,
            SeenPost.raw_text.is_not(None),
            SeenPost.triage_status == "done",
        )
        .values(raw_text=None, raw_purged_at=stamp, raw_purge_after=None)
    )
    db.flush()
    db.expire_all()
    return int(result.rowcount or 0)


__all__ = [
    "Persisted",
    "Sighting",
    "attach_permalink",
    "fingerprint",
    "list_skipped",
    "persist_triage",
    "purge_expired",
    "register_sighting",
    "rescue",
    "retry_triage",
    "skipped_counts",
    "snippet_of",
    "triage_inputs",
]

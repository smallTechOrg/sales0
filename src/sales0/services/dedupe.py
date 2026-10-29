"""Dedupe: one enquiry per real-world post (architecture.md → Identity and dedupe).

Match order, first hit wins:
1. same source ref (a LinkedIn URN, or ``ck:<componentkey>`` when the page exposes none) → ``dupe_urn``;
2. same normalised content hash → ``dupe_content``;
3. same author profile slug + same opening ~300 normalised chars (``near_duplicate``) → ``dupe_content``;
4. otherwise a new enquiry.

Duplicates add their keyword hit and any unseen source ref (incl. a URN learned later) to the
existing enquiry, upgrade a profile/search link to a permalink, and are never re-triaged.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, EnquiryKeywordHit, EnquirySource
from sales0.scraper.types import ScrapedPost
from sales0.scraper.urls import link_kind_of, profile_slug
from sales0.services._common import clip, now_or

UpsertKind = Literal["new", "dupe_urn", "dupe_content"]

# Full URLs, www. links, and scheme-less short links (lnkd.in/abc, bit.ly/x, t.co/x ...):
# a bare ``domain.tld/path`` token. Requires the slash so ordinary "e.g." / "node.js" survive.
_URL = re.compile(
    r"(?:https?://|www\.)\S+|(?<![\w@.])(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*",
    re.IGNORECASE,
)
# The numeric LinkedIn post id (urn:li:activity:N, /posts/x-activity-N-h, ugcPost/share variants). The same
# pattern is frozen in migration p9_activity_d4a8c2e6 and used by the sheet sync (Enquiry.activity_id).
ACTIVITY_RE = re.compile(r"(?:activity|ugcPost|share)[-:](\d{6,})", re.IGNORECASE)
_HASHTAG = re.compile(r"#[^\s#]+")
_WS = re.compile(r"\s+")


def activity_id_of(*texts: str | None) -> str | None:
    """The first LinkedIn activity id found in ``texts`` (URLs / URNs), or None."""
    for t in texts:
        if t:
            m = ACTIVITY_RE.search(t)
            if m:
                return m.group(1)
    return None


def normalize_text(text: str) -> str:
    """NFKC, lower-case, URLs removed, hashtags removed, whitespace collapsed."""
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = _URL.sub(" ", value)
    value = _HASHTAG.sub(" ", value)
    return _WS.sub(" ", value).strip()


def content_hash(text: str) -> str:
    """sha256 hex of ``normalize_text(text)``."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


_TRAILING_MORE = re.compile(r"\s*(?:…|\.\.\.)\s*(?:see\s+)?more\s*$", re.IGNORECASE)
_TRAILING_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*$")

FINGERPRINT_CHARS = 300  # compare at most the first N normalised characters
FINGERPRINT_MIN_OVERLAP = 100  # ...and require at least this many to agree


def strip_ellipsis(text: str) -> str:
    """Drop a trailing "…more" (LinkedIn's expand button text) and a trailing "…" / "..."."""
    value = _TRAILING_MORE.sub("", text or "")
    return _TRAILING_ELLIPSIS.sub("", value)


def _fingerprint(text: str) -> str:
    return normalize_text(strip_ellipsis(text))[:FINGERPRINT_CHARS]


def near_duplicate(
    a_profile_url: str | None, a_text: str, b_profile_url: str | None, b_text: str
) -> bool:
    """Same author (profile slug) and the same opening text — e.g. one sighting read truncated
    ("…more") and one expanded. Pure (architecture.md → Identity and dedupe, step 3)."""
    slug_a, slug_b = profile_slug(a_profile_url), profile_slug(b_profile_url)
    if slug_a is None or slug_a != slug_b:
        return False
    a, b = _fingerprint(a_text), _fingerprint(b_text)
    n = min(len(a), len(b))
    return n >= FINGERPRINT_MIN_OVERLAP and a[:n] == b[:n]


def _hash_for(post: ScrapedPost) -> str:
    # A post whose text normalises to nothing must not merge with every other empty post.
    if not normalize_text(post.text):
        return hashlib.sha256(f"empty:{post.source_ref}".encode()).hexdigest()
    return content_hash(post.text)


def _source_url(post: ScrapedPost) -> str | None:
    """enquiry_sources.source_url: the permalink when known, else the author's profile, else (only if neither)
    the search page it was seen on (inbox feedback A2: never a search page when a profile is known)."""
    if post.link_kind == "post":
        return post.url
    if post.author_profile_url:
        return post.author_profile_url
    return post.url or post.search_url


def _by_activity_id(db: Session, activity: str | None) -> int | None:
    """Indexed lookup (``enquiries.activity_id``) replacing the old ``LIKE '%id%'`` scan."""
    if not activity:
        return None
    return db.scalar(select(Enquiry.id).where(Enquiry.activity_id == activity).order_by(Enquiry.id).limit(1))


def _near_duplicate_of(db: Session, post: ScrapedPost) -> int | None:
    activity = activity_id_of(post.urn, post.url, post.source_ref)
    by_activity = _by_activity_id(db, activity)
    if by_activity is not None:
        return by_activity  # same LinkedIn post id = same post, whatever the author URL looks like
    slug = profile_slug(post.author_profile_url)
    if slug is None or not normalize_text(post.text):
        return None
    candidates = db.execute(
        select(Enquiry.id, Enquiry.author_profile_url, Enquiry.raw_text)
        .where(Enquiry.author_profile_url.contains(f"/{slug}", autoescape=True))
        .order_by(Enquiry.id)
    ).all()
    for enquiry_id, profile_url, raw_text in candidates:
        if near_duplicate(profile_url, raw_text, post.author_profile_url, post.text):
            return int(enquiry_id)
    return None


def _ref_exists(db: Session, ref: str) -> bool:
    return (
        db.scalar(select(EnquirySource.id).where(EnquirySource.source_ref == ref))
        is not None
    )


def _merge(
    db: Session, enquiry_id: int, post: ScrapedPost, run_id: int, now: datetime
) -> None:
    """Rules on every duplicate: new source row(s) for unseen refs (incl. the URN alias) and the
    profile → permalink link upgrade. Text, author and hash are never rewritten."""
    refs: list[str] = []
    for ref in (post.source_ref, post.urn):
        if ref and ref not in refs and not _ref_exists(db, ref):
            refs.append(ref)
    for ref in refs:
        db.add(
            EnquirySource(
                enquiry_id=enquiry_id,
                source_ref=ref,
                source_url=_source_url(post),
                first_seen_run_id=run_id,
                first_seen_at=now,
            )
        )
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is not None and enquiry.activity_id is None:
        enquiry.activity_id = activity_id_of(post.urn, post.url, post.source_ref)
    if post.link_kind == "post" and enquiry is not None and link_kind_of(enquiry.source_url) != "post":
        enquiry.source_url = post.url


def _add_hit(
    db: Session,
    enquiry_id: int,
    run_id: int,
    keyword_id: int | None,
    keyword_text: str,
    now: datetime,
) -> None:
    exists = db.scalar(
        select(EnquiryKeywordHit.id).where(
            EnquiryKeywordHit.enquiry_id == enquiry_id,
            EnquiryKeywordHit.run_id == run_id,
            EnquiryKeywordHit.keyword_text == keyword_text,
        )
    )
    if exists is None:
        db.add(
            EnquiryKeywordHit(
                enquiry_id=enquiry_id,
                run_id=run_id,
                keyword_id=keyword_id,
                keyword_text=keyword_text,
                seen_at=now,
            )
        )


def upsert_post(
    db: Session,
    post: ScrapedPost,
    run_id: int,
    keyword_id: int | None,
    keyword_text: str,
    *,
    now: datetime | None = None,
) -> tuple[UpsertKind, int]:
    """Insert or merge ``post``; returns (kind, enquiry_id). Flushes, does not commit."""
    now = now_or(now)
    # 1. Same source ref (URN or ck:<KEY>).
    matched = db.scalar(
        select(EnquirySource.enquiry_id).where(
            EnquirySource.source_ref == post.source_ref
        )
    )
    kind: UpsertKind = "dupe_urn"
    if matched is None:
        kind = "dupe_content"
        # 2. Same normalised content.
        matched = db.scalar(
            select(Enquiry.id)
            .where(Enquiry.content_hash == _hash_for(post))
            .order_by(Enquiry.id)
        )
        if matched is None:
            # 3. Same author + same opening text (truncated vs expanded sighting).
            matched = _near_duplicate_of(db, post)
    if matched is not None:
        enquiry_id = int(matched)
        _merge(db, enquiry_id, post, run_id, now)
        _add_hit(db, enquiry_id, run_id, keyword_id, keyword_text, now)
        db.flush()
        return kind, enquiry_id

    digest = _hash_for(post)
    enquiry = Enquiry(
        source="linkedin_post",
        content_hash=digest,
        raw_text=post.text,
        author_name=clip(post.author_name, 200),
        author_headline=clip(post.author_headline, 500),
        author_profile_url=post.author_profile_url,
        source_url=post.url,
        activity_id=activity_id_of(post.urn, post.url, post.source_ref),
        date_posted=post.posted_date,
        date_added=now,
        review_status="pending_review",
        triage_status="pending",
    )
    db.add(enquiry)
    db.flush()
    for ref in dict.fromkeys(r for r in (post.source_ref, post.urn) if r):
        db.add(
            EnquirySource(
                enquiry_id=enquiry.id,
                source_ref=ref,
                source_url=_source_url(post),
                first_seen_run_id=run_id,
                first_seen_at=now,
            )
        )
    _add_hit(db, enquiry.id, run_id, keyword_id, keyword_text, now)
    db.flush()
    return "new", enquiry.id


__all__ = [
    "ACTIVITY_RE",
    "activity_id_of",
    "content_hash",
    "near_duplicate",
    "normalize_text",
    "strip_ellipsis",
    "upsert_post",
]

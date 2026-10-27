"""Dedupe: one enquiry per real-world post.

A post whose LinkedIn URN was seen before is a URN duplicate. A post with a new URN but
the same normalised text as an existing enquiry is a content duplicate. Duplicates add
their keyword hit (and, for content duplicates, their new source ref) to the existing
enquiry; they never create a second enquiry and are never re-triaged.
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
from sales0.services._common import clip, now_or

UpsertKind = Literal["new", "dupe_urn", "dupe_content"]

# Full URLs, www. links, and scheme-less short links (lnkd.in/abc, bit.ly/x, t.co/x ...):
# a bare ``domain.tld/path`` token. Requires the slash so ordinary "e.g." / "node.js" survive.
_URL = re.compile(
    r"(?:https?://|www\.)\S+|(?<![\w@.])(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*",
    re.IGNORECASE,
)
_HASHTAG = re.compile(r"#[^\s#]+")
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """NFKC, lower-case, URLs removed, hashtags removed, whitespace collapsed."""
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = _URL.sub(" ", value)
    value = _HASHTAG.sub(" ", value)
    return _WS.sub(" ", value).strip()


def content_hash(text: str) -> str:
    """sha256 hex of ``normalize_text(text)``."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _hash_for(post: ScrapedPost) -> str:
    # A post whose text normalises to nothing must not merge with every other empty post.
    if not normalize_text(post.text):
        return hashlib.sha256(f"empty:{post.urn}".encode()).hexdigest()
    return content_hash(post.text)


def _add_hit(db: Session, enquiry_id: int, run_id: int, keyword_id: int | None, keyword_text: str, now: datetime) -> None:
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
                enquiry_id=enquiry_id, run_id=run_id, keyword_id=keyword_id, keyword_text=keyword_text, seen_at=now
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
    source = db.scalar(select(EnquirySource).where(EnquirySource.source_ref == post.urn))
    if source is not None:
        _add_hit(db, source.enquiry_id, run_id, keyword_id, keyword_text, now)
        db.flush()
        return "dupe_urn", source.enquiry_id

    digest = _hash_for(post)
    existing_id = db.scalar(select(Enquiry.id).where(Enquiry.content_hash == digest))
    if existing_id is not None:
        db.add(
            EnquirySource(
                enquiry_id=existing_id,
                source_ref=post.urn,
                source_url=post.url,
                first_seen_run_id=run_id,
                first_seen_at=now,
            )
        )
        _add_hit(db, existing_id, run_id, keyword_id, keyword_text, now)
        db.flush()
        return "dupe_content", existing_id

    enquiry = Enquiry(
        source="linkedin_post",
        content_hash=digest,
        raw_text=post.text,
        author_name=clip(post.author_name, 200),
        author_headline=clip(post.author_headline, 500),
        author_profile_url=post.author_profile_url,
        source_url=post.url,
        date_posted=post.posted_date,
        date_added=now,
        review_status="pending_review",
        triage_status="pending",
    )
    db.add(enquiry)
    db.flush()
    db.add(
        EnquirySource(
            enquiry_id=enquiry.id, source_ref=post.urn, source_url=post.url, first_seen_run_id=run_id, first_seen_at=now
        )
    )
    _add_hit(db, enquiry.id, run_id, keyword_id, keyword_text, now)
    db.flush()
    return "new", enquiry.id


__all__ = ["content_hash", "normalize_text", "upsert_post"]

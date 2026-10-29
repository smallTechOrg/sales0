"""LinkedIn author identities (Phase 1.4): one row per distinct profile, upserted on every sighting."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sales0.db.models import LinkedinProfile
from sales0.scraper.urls import profile_slug
from sales0.services._common import clip, now_or
from sales0.services.crm._keys import linkedin_slug_of
from sales0.services.views import ProfileView

LIVE_BASE = "https://www.linkedin.com"


def _canonical_url(slug: str) -> str:
    return f"{LIVE_BASE}/{slug}/"


def touch_profile(
    db: Session,
    *,
    profile_url: str | None,
    name: str | None,
    headline: str | None,
    now: datetime | None = None,
) -> int | None:
    """Upsert by ``profile_slug``; +1 ``posts_seen`` per sighting. None when the URL has no slug."""
    slug = profile_slug(profile_url)
    if slug is None:
        return None
    stamp = now_or(now)
    row = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == slug))
    display = clip(name, 200)
    head = clip(headline, 500)
    if row is None:
        row = LinkedinProfile(
            slug=slug,
            kind="company" if slug.startswith("company/") else "person",
            profile_url=_canonical_url(slug)[:500],
            display_name=display,
            headline=head,
            first_seen_at=stamp,
            last_seen_at=stamp,
            posts_seen=1,
            relevant_count=0,
        )
        db.add(row)
        db.flush()
        return row.id
    row.posts_seen = (row.posts_seen or 0) + 1
    row.last_seen_at = stamp
    if display is not None:
        row.display_name = display
    if head is not None:
        row.headline = head
    db.flush()
    return row.id


def ensure_profile(
    db: Session,
    profile_url: str | None,
    *,
    name: str | None = None,
    headline: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """Find or create the profile row for ``profile_url`` WITHOUT counting a sighting.

    Unlike ``touch_profile`` this never increments ``posts_seen`` (the CRM calls it for sheet rows,
    pasted posts and manual prospects, which are not scraper sightings). Fills an empty
    ``display_name`` / ``headline`` of an existing row. Returns None when the URL has no slug.
    """
    slug = linkedin_slug_of(profile_url)
    if slug is None:
        return None
    row = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == slug))
    display, head = clip(name, 200), clip(headline, 500)
    if row is None:
        stamp = now_or(now)
        row = LinkedinProfile(
            slug=slug,
            kind="company" if slug.startswith("company/") else "person",
            profile_url=_canonical_url(slug)[:500],
            display_name=display,
            headline=head,
            first_seen_at=stamp,
            last_seen_at=stamp,
            posts_seen=0,
            relevant_count=0,
        )
        db.add(row)
        db.flush()
        return row.id
    changed = False
    if display and not row.display_name:
        row.display_name, changed = display, True
    if head and not row.headline:
        row.headline, changed = head, True
    if changed:
        db.flush()
    return row.id


def mark_relevant(db: Session, profile_id: int | None, n: int = 1) -> None:
    """``relevant_count += n`` (Core UPDATE, so concurrent sessions never lose an increment)."""
    if profile_id is None or n == 0:
        return
    db.execute(
        update(LinkedinProfile)
        .where(LinkedinProfile.id == profile_id)
        .values(relevant_count=LinkedinProfile.relevant_count + n)
    )
    db.flush()
    db.expire_all()


def _view(row: LinkedinProfile) -> ProfileView:
    return ProfileView(
        id=row.id,
        slug=row.slug,
        kind=row.kind,  # type: ignore[arg-type]
        profile_url=row.profile_url,
        display_name=row.display_name,
        headline=row.headline,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        posts_seen=row.posts_seen,
        relevant_count=row.relevant_count,
        tracked=row.tracked,
    )


def get_profile(db: Session, slug: str) -> ProfileView | None:
    """Look up by slug (``in/<x>``) or by a profile URL; None when unknown."""
    key = profile_slug(slug) if "/" in slug and not slug.startswith(("in/", "company/")) else slug.strip().lower()
    row = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == key))
    return _view(row) if row is not None else None


__all__ = ["ensure_profile", "get_profile", "mark_relevant", "touch_profile"]

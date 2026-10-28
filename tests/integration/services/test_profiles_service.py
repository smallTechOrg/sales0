"""LinkedIn author identities on real SQLite: upsert per sighting, counters, slug normalisation."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.db.models import LinkedinProfile
from sales0.services import profiles


def test_touch_profile_upserts_by_slug_and_counts_every_sighting(db: Session) -> None:
    t0 = utcnow()
    first = profiles.touch_profile(
        db, profile_url="https://www.linkedin.com/in/Priya-Menon/?miniProfileUrn=x", name="Priya", headline=None, now=t0
    )
    again = profiles.touch_profile(
        db, profile_url="https://linkedin.com/in/priya-menon", name=None, headline="Head of L&D", now=t0 + timedelta(hours=1)
    )
    assert first is not None and again == first
    view = profiles.get_profile(db, "in/priya-menon")
    assert view is not None
    assert (view.kind, view.profile_url, view.posts_seen, view.relevant_count) == (
        "person", "https://www.linkedin.com/in/priya-menon/", 2, 0)
    assert (view.display_name, view.headline, view.tracked) == ("Priya", "Head of L&D", None)
    assert view.last_seen_at == t0 + timedelta(hours=1) and view.first_seen_at == t0
    assert db.scalar(select(func.count(LinkedinProfile.id))) == 1


def test_touch_profile_keeps_older_name_when_new_sighting_has_none_and_overwrites_when_present(db: Session) -> None:
    url = "https://www.linkedin.com/company/acme-corp/"
    pid = profiles.touch_profile(db, profile_url=url, name="Acme", headline="We train", now=None)
    profiles.touch_profile(db, profile_url=url, name=None, headline=None)
    profiles.touch_profile(db, profile_url=url, name="Acme Corp", headline=None)
    view = profiles.get_profile(db, "company/acme-corp")
    assert pid is not None and view is not None
    assert (view.kind, view.display_name, view.headline, view.posts_seen) == ("company", "Acme Corp", "We train", 3)


def test_touch_profile_returns_none_without_a_profile_slug(db: Session) -> None:
    for url in (None, "", "https://www.linkedin.com/feed/", "https://www.linkedin.com/search/results/content/?keywords=x"):
        assert profiles.touch_profile(db, profile_url=url, name="N", headline=None) is None
    assert db.scalar(select(func.count(LinkedinProfile.id))) == 0


def test_mark_relevant_adds_n_and_ignores_none(db: Session) -> None:
    pid = profiles.touch_profile(db, profile_url="https://www.linkedin.com/in/a-b/", name=None, headline=None)
    profiles.mark_relevant(db, pid)
    profiles.mark_relevant(db, pid, 2)
    profiles.mark_relevant(db, None)
    view = profiles.get_profile(db, "in/a-b")
    assert view is not None and view.relevant_count == 3


def test_get_profile_unknown_slug_is_none_and_accepts_a_url(db: Session) -> None:
    assert profiles.get_profile(db, "in/nobody") is None
    profiles.touch_profile(db, profile_url="https://www.linkedin.com/in/known/", name="K", headline=None)
    assert profiles.get_profile(db, "https://www.linkedin.com/in/known/") is not None

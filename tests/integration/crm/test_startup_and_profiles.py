"""startup_backfill (once per DB) and profiles.ensure_profile."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from sales0.db.models import AppMeta, Flag, LinkedinProfile
from sales0.services import crm, profiles
from sales0.services.crm import flags, prospects
from tests.integration.crm import helpers as h


def test_startup_backfill_flags_old_data_once(db) -> None:
    prof = h.profile(db)
    from sales0.clock import utcnow

    es = [h.enquiry(db, profile_row=prof, company="Repeat Co", date_added=utcnow() - timedelta(days=i)) for i in range(3)]
    prospects.ensure_for_enquiry(db, es[0].id)
    # wipe the flags the accept path raised, as if the data pre-dates the CRM
    db.query(Flag).delete()
    db.flush()
    h.enquiry(db, company="Q", raw_text="we used Savya Technologies before")
    assert crm.startup_backfill(db) is None
    kinds = {f.kind for f in db.scalars(select(Flag).where(Flag.cleared_at.is_(None)))}
    assert kinds == {"repeat_poster", "competitor_mention"}
    assert db.get(AppMeta, crm.BACKFILL_KEY).value == "1"
    # second run does nothing, even though a flag was removed meanwhile
    db.query(Flag).delete()
    db.flush()
    crm.startup_backfill(db)
    assert db.scalar(select(func.count(Flag.id))) == 0


def test_startup_backfill_on_an_empty_database_just_sets_the_marker(db) -> None:
    crm.startup_backfill(db)
    assert db.get(AppMeta, crm.BACKFILL_KEY) is not None and flags.recompute_all(db) == {"repeat_poster": 0, "competitor_mention": 0}


def test_ensure_profile_creates_without_counting_a_sighting(db) -> None:
    pid = profiles.ensure_profile(db, "https://www.linkedin.com/in/New-Person/?utm=1", name="New Person", headline="CHRO")
    row = db.get(LinkedinProfile, pid)
    assert row.slug == "in/new-person" and row.kind == "person" and row.posts_seen == 0 and row.relevant_count == 0
    assert row.display_name == "New Person" and row.headline == "CHRO" and row.tracked is None
    assert row.profile_url == "https://www.linkedin.com/in/new-person/"


def test_ensure_profile_is_idempotent_fills_blanks_and_never_overwrites(db) -> None:
    a = profiles.ensure_profile(db, "https://www.linkedin.com/in/x-y/")
    b = profiles.ensure_profile(db, "linkedin.com/in/X-Y", name="X Y", headline="Head")
    c = profiles.ensure_profile(db, "https://www.linkedin.com/in/x-y/", name="Other Name")
    assert a == b == c
    row = db.get(LinkedinProfile, a)
    assert row.display_name == "X Y" and row.headline == "Head" and row.posts_seen == 0
    assert db.scalar(select(func.count(LinkedinProfile.id))) == 1


def test_ensure_profile_company_pages_and_non_profiles(db) -> None:
    cid = profiles.ensure_profile(db, "https://www.linkedin.com/company/acme-corp/")
    assert db.get(LinkedinProfile, cid).kind == "company" and db.get(LinkedinProfile, cid).slug == "company/acme-corp"
    for bad in (None, "", "   ", "https://www.linkedin.com/feed/", "https://example.com/in/someone", "not a url"):
        assert profiles.ensure_profile(db, bad) is None
    assert db.scalar(select(func.count(LinkedinProfile.id))) == 1


def test_ensure_profile_accepts_the_fixture_linkedin(db) -> None:
    pid = profiles.ensure_profile(db, "http://127.0.0.1:9999/__fixtures/linkedin/in/fixture-person/")
    assert db.get(LinkedinProfile, pid).slug == "in/fixture-person"
    assert db.get(LinkedinProfile, pid).profile_url == "https://www.linkedin.com/in/fixture-person/"

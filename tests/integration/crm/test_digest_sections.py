"""CRM digest sections: exact counts, ordering, caps, savepoint isolation, registration."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import Company, Digest, Enquiry, FollowUp, Lead, Prospect, TimelineEvent
from sales0.services import digest as dg
from sales0.services.crm import digest_sections, followups
from tests.integration.digest.conftest import NOW, hours_ago

TODAY = date(2026, 10, 28)  # NOW is 17:30 on this day in Asia/Kolkata
KEYS = ("follow_ups_due", "stale_leads", "pipeline_movement")


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture(autouse=True)
def registered() -> Iterator[None]:
    digest_sections.register()
    yield
    for k in KEYS:
        dg.unregister_section(k)


def section(db: Session, key: str, now: datetime = NOW) -> dg.SectionView:
    view = dg.generate_digest(db, now=now, with_summary=False)
    db.commit()
    return next(s for s in view.sections if s.key == key)


def texts(sec: dg.SectionView) -> list[str]:
    return [dg.item_text(i) for i in sec.items]


def prospect(db: Session, name: str, company: str | None = None, **kw: object) -> Prospect:
    cid = None
    if company:
        c = db.scalars(select(Company).where(Company.name == company)).first()
        if c is None:
            c = Company(name=company, name_key=company.casefold())
            db.add(c)
            db.flush()
        cid = c.id
    # created_at is pinned to the fixed NOW: the wall clock moves past NOW's day and would fall outside the digest window.
    kw.setdefault("created_at", hours_ago(1))
    p = Prospect(name=name, company_id=cid, **kw)
    db.add(p)
    db.commit()
    return p


def reminder(db: Session, p: Prospect, due: date, kind: str = "follow_up_1", **kw: object) -> FollowUp:
    f = FollowUp(prospect_id=p.id, kind=kind, due_on=due, auto=True, **kw)
    db.add(f)
    db.commit()
    return f


def lead(db: Session, p: Prospect, *, stage: str = "proposal_sent", opened: datetime | None = None, **kw: object) -> Lead:
    row = Lead(prospect_id=p.id, company_id=p.company_id, stage=stage, opened_at=opened or hours_ago(24 * 40), **kw)
    db.add(row)
    db.commit()
    return row


def event(db: Session, p: Prospect, at: datetime, kind: str = "note") -> None:
    db.add(TimelineEvent(subject_type="prospect", subject_id=p.id, prospect_id=p.id, kind=kind, actor="Ann", occurred_at=at))
    db.commit()


# ------------------------------------------------------------------------------ registration
def test_the_three_sections_are_registered_in_order_and_registration_is_idempotent(db: Session) -> None:
    digest_sections.register()
    digest_sections.register()
    secs = dg.sections()
    keys = [s.key for s in secs]
    assert keys.count("follow_ups_due") == 1
    assert [(s.key, s.order, s.title) for s in secs if s.key in KEYS] == [
        ("follow_ups_due", 25, "Follow-ups due"), ("stale_leads", 26, "Stale leads"), ("pipeline_movement", 27, "Pipeline movement"),  # noqa: E501
    ]
    assert keys.index("awaiting_review") < keys.index("follow_ups_due") < keys.index("outreach")
    assert keys.index("pipeline_movement") < keys.index("outreach")


# ------------------------------------------------------------------------------ follow-ups due
def test_two_due_today_and_one_overdue_are_listed_overdue_first(db: Session) -> None:
    acme = prospect(db, "Priya Menon", "Acme Training")
    beta = prospect(db, "Bala Iyer", "Beta Corp")
    gamma = prospect(db, "Gita Rao", "Gamma Ltd")
    reminder(db, acme, TODAY, "follow_up_1")
    reminder(db, beta, TODAY, "follow_up_2")
    reminder(db, gamma, TODAY - timedelta(days=3), "final_follow_up")
    reminder(db, acme, TODAY + timedelta(days=1), "final_follow_up")  # tomorrow: not due
    sec = section(db, "follow_ups_due")
    assert sec.stats == {"due_today": 2, "overdue": 1} and sec.error is None
    assert len(sec.items) == 3
    assert texts(sec)[0] == "Gamma Ltd - Final follow-up overdue 3d (Gita Rao)"
    assert sorted(texts(sec)[1:]) == [
        "Acme Training - Follow-up 1 due today (Priya Menon)", "Beta Corp - Follow-up 2 due today (Bala Iyer)",
    ]
    hrefs = {dg.item_href(i) for i in sec.items}
    assert hrefs == {f"/pipeline/prospects/{p.id}" for p in (acme, beta, gamma)}


def test_done_cancelled_and_future_reminders_are_ignored_and_empty_has_text(db: Session) -> None:
    p = prospect(db, "Solo Person")
    reminder(db, p, TODAY, done_at=NOW)
    reminder(db, p, TODAY, cancelled_at=NOW, cancel_reason="response")
    reminder(db, p, TODAY + timedelta(days=5))
    sec = section(db, "follow_ups_due")
    assert sec.items == [] and sec.stats == {"due_today": 0, "overdue": 0}
    assert sec.empty_text == "No follow-ups due."


def test_the_list_is_capped_at_10_but_the_stats_count_everything(db: Session) -> None:
    for i in range(12):
        reminder(db, prospect(db, f"Person {i}"), TODAY - timedelta(days=1 + i % 3))
    sec = section(db, "follow_ups_due")
    assert len(sec.items) == 10 and sec.stats["overdue"] == 12 and sec.stats["due_today"] == 0
    assert texts(sec)[0].endswith("overdue 3d")
    days = [int(t.split("overdue ")[1].split("d")[0]) for t in texts(sec)]
    assert days == sorted(days, reverse=True)  # the most overdue first


def test_today_is_the_local_date_of_the_window_end(db: Session) -> None:
    p = prospect(db, "Late Night", "Night Co")
    reminder(db, p, TODAY + timedelta(days=1))
    assert section(db, "follow_ups_due").stats == {"due_today": 0, "overdue": 0}
    next_morning = NOW + timedelta(hours=14)  # 07:30 IST on the 29th
    sec = section(db, "follow_ups_due", next_morning)
    assert sec.stats == {"due_today": 1, "overdue": 0}


def test_a_lead_reminder_links_to_the_lead(db: Session) -> None:
    p = prospect(db, "Lead Person", "Lead Co", status="converted")
    row = lead(db, p)
    db.add(FollowUp(lead_id=row.id, kind="custom", due_on=TODAY, note="call back"))
    db.commit()
    sec = section(db, "follow_ups_due")
    assert dg.item_href(sec.items[0]) == f"/pipeline/leads/{row.id}" and sec.stats["due_today"] == 1


# ------------------------------------------------------------------------------ stale leads
def test_a_lead_quiet_for_19_days_is_stale_and_one_quiet_for_5_is_not(db: Session) -> None:
    quiet = prospect(db, "Quiet Person", "Acme", status="converted")
    fresh = prospect(db, "Fresh Person", "Beta", status="converted")
    l_quiet = lead(db, quiet, stage="proposal_sent")
    lead(db, fresh, stage="meeting_scheduled")
    event(db, quiet, NOW - timedelta(days=30))
    event(db, quiet, NOW - timedelta(days=19))  # the NEWEST event decides
    event(db, fresh, NOW - timedelta(days=5))
    sec = section(db, "stale_leads")
    assert texts(sec) == ["Acme - Proposal sent, quiet for 19 days"]
    assert dg.item_href(sec.items[0]) == f"/pipeline/leads/{l_quiet.id}"
    assert sec.stats == {"stale": 1, "open_leads": 2}


def test_stale_boundary_closed_leads_and_leads_without_events(db: Session) -> None:
    edge = prospect(db, "Edge", "Edge Co", status="converted")
    just_in = prospect(db, "JustIn", "In Co", status="converted")
    closed = prospect(db, "Closed", "Closed Co", status="converted")
    no_events = prospect(db, "NoEvents", "Old Co", status="converted")
    lead(db, edge)
    lead(db, just_in)
    lead(db, closed, stage="lost", closed_at=NOW - timedelta(days=40))
    lead(db, no_events, stage="meeting_scheduled", opened=NOW - timedelta(days=20))
    event(db, edge, NOW - timedelta(days=14, seconds=1))  # older than 14 days: stale
    event(db, just_in, NOW - timedelta(days=14))  # exactly 14 days: not older than the threshold
    event(db, closed, NOW - timedelta(days=60))
    sec = section(db, "stale_leads")
    assert texts(sec) == ["Old Co - Meeting scheduled, quiet for 20 days", "Edge Co - Proposal sent, quiet for 14 days"]
    assert sec.stats == {"stale": 2, "open_leads": 3}


def test_sheet_removed_rows_are_not_stale_leads(db: Session) -> None:
    gone_person = prospect(db, "Gone Person", "Gone Co", status="converted", sheet_removed_at=NOW - timedelta(days=1))
    gone_enq = prospect(db, "Gone Enq", "Enq Co", status="converted")
    live_enq = prospect(db, "Live Enq", "Live Co", status="converted")
    for p in (gone_person, gone_enq, live_enq):
        lead(db, p, stage="proposal_sent")
        event(db, p, NOW - timedelta(days=30))
    db.add(Enquiry(raw_text="x", content_hash="h-gone", source="linkedin_post", prospect_id=gone_enq.id, sheet_removed_at=NOW))
    db.add(Enquiry(raw_text="y", content_hash="h-live", source="linkedin_post", prospect_id=live_enq.id))
    db.commit()
    sec = section(db, "stale_leads")
    assert texts(sec) == ["Live Co - Proposal sent, quiet for 30 days"]
    assert sec.stats == {"stale": 1, "open_leads": 1}


def test_the_threshold_comes_from_settings(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    p = prospect(db, "Slow", "Slow Co", status="converted")
    lead(db, p)
    event(db, p, NOW - timedelta(days=5))
    assert section(db, "stale_leads").stats["stale"] == 0
    monkeypatch.setenv("SALES0_STALE_LEAD_DAYS", "3")
    reset_settings()
    assert section(db, "stale_leads").stats["stale"] == 1


def test_no_leads_gives_the_empty_text(db: Session) -> None:
    sec = section(db, "stale_leads")
    assert sec.items == [] and sec.stats == {"stale": 0, "open_leads": 0} and sec.empty_text == "No stale leads."


# ------------------------------------------------------------------------------ pipeline movement
def test_pipeline_movement_counts_the_24h_window_and_skips_sheet_rows(db: Session) -> None:
    new = prospect(db, "New One", source="enquiry")
    prospect(db, "Sheet One", source="sheet")
    old = prospect(db, "Old One", source="enquiry")
    old.created_at = hours_ago(30)
    contacted = prospect(db, "Contacted", source="enquiry", contacted_at=hours_ago(2))
    prospect(db, "Estimated", source="sheet", contacted_at=hours_ago(2), contacted_estimated=True)
    db.commit()
    opened = lead(db, new, opened=hours_ago(3), opened_by="reply")
    won = lead(db, contacted, stage="won", opened=hours_ago(50), closed_at=hours_ago(1))
    lead(db, prospect(db, "Sheet Lead"), opened=hours_ago(1), opened_by="sheet")
    event(db, new, hours_ago(1), "reply_received")
    event(db, new, hours_ago(40), "reply_received")
    assert opened.id and won.id
    sec = section(db, "pipeline_movement")
    assert sec.stats == {"new_prospects": 3, "contacted": 1, "replies": 1, "leads_opened": 1, "won": 1}
    assert texts(sec) == ["3 new prospects", "1 contacted", "1 reply", "1 lead opened", "1 won"]


def test_an_empty_pipeline_says_so(db: Session) -> None:
    sec = section(db, "pipeline_movement")
    assert sec.items == [] and set(sec.stats.values()) == {0} and "No pipeline movement" in sec.empty_text


# ------------------------------------------------------------------------------ isolation
def test_a_failing_crm_section_shows_an_error_item_and_the_rest_of_the_digest_is_intact(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = prospect(db, "Priya", "Acme")
    reminder(db, acme, TODAY)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("crm exploded")

    monkeypatch.setattr(followups, "list_due", boom)
    view = dg.generate_digest(db, now=NOW, with_summary=False)
    db.commit()
    by_key = {s.key: s for s in view.sections}
    assert by_key["follow_ups_due"].error and "RuntimeError" in by_key["follow_ups_due"].error
    assert "could not be built" in dg.item_text(by_key["follow_ups_due"].items[0])
    assert by_key["stale_leads"].error is None and by_key["pipeline_movement"].error is None
    assert by_key["awaiting_review"].error is None and by_key["outreach"].error is None
    assert db.scalar(select(func.count()).select_from(Digest)) == 1


def test_a_half_written_section_is_rolled_back_by_its_savepoint(db: Session) -> None:
    def writes_then_fails(session: Session, w: dg.Window) -> dg.SectionResult:
        session.add(Company(name="Ghost", name_key="ghost"))
        session.flush()
        raise RuntimeError("late failure")

    dg.register_section(dg.DigestSection("zz_ghost", "Ghost", 99, writes_then_fails))
    try:
        view = dg.generate_digest(db, now=NOW, with_summary=False)
        db.commit()
    finally:
        dg.unregister_section("zz_ghost")
    assert next(s for s in view.sections if s.key == "zz_ghost").error
    assert db.scalar(select(func.count()).select_from(Company).where(Company.name == "Ghost")) == 0
    assert next(s for s in view.sections if s.key == "follow_ups_due").error is None


def test_the_digest_text_and_state_survive_a_reload(db: Session) -> None:
    p = prospect(db, "Priya Menon", "Acme Training")
    reminder(db, p, TODAY)
    built = dg.generate_digest(db, now=NOW, with_summary=False)
    db.commit()
    db.expire_all()
    loaded = dg.get_digest(db, built.id)
    sec = next(s for s in loaded.sections if s.key == "follow_ups_due")
    assert sec.stats == {"due_today": 1, "overdue": 0}
    assert "Acme Training - Follow-up 1 due today (Priya Menon)" in dg.digest_text(loaded)

"""Digest service on a migrated SQLite DB: exact window counts, isolation, upsert, summary guards."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.config import get_settings, reset_settings
from sales0.db.models import Digest, LlmCall
from sales0.llm import LLMTransientError
from sales0.services import digest as dg
from tests.integration.digest.conftest import NOW, Team, add_enquiry, add_llm_call, add_message, add_run, hours_ago
from tests.support.fake_llm import FakeLLMClient


def section(view: dg.DigestView, key: str) -> dg.SectionView:
    return next(s for s in view.sections if s.key == key)


def test_empty_database_builds_every_section_with_empty_text(db: Session) -> None:
    view = dg.generate_digest(db, now=NOW, with_summary=False)
    assert [s.key for s in view.sections] == [s.key for s in dg.sections()]
    assert section(view, "new_relevant_enquiries").items == []
    assert section(view, "new_relevant_enquiries").stats["new_relevant"] == 0
    assert section(view, "outreach").stats == {"drafted": 0, "approved": 0, "sent": 0, "failed": 0, "stuck_sending": 0}
    assert view.summary_text is None and view.digest_date.isoformat() == "2026-10-28"
    assert "No discovery runs" in section(view, "scrape_health").empty_text


def test_new_enquiries_use_exact_24h_boundaries_and_only_triaged_genuine(db: Session) -> None:
    add_enquiry(db, added=hours_ago(24), company="OnStart")  # exactly 24h ago: in
    add_enquiry(db, added=NOW, company="OnEnd", priority="low")  # exactly now: in
    add_enquiry(db, added=hours_ago(24) - timedelta(seconds=1), company="TooOld")
    add_enquiry(db, added=NOW + timedelta(seconds=1), company="Future")
    add_enquiry(db, added=hours_ago(1), verdict="discard", company="Discarded")
    add_enquiry(db, added=hours_ago(1), triage="pending", verdict=None, company="Untriaged")
    add_enquiry(db, added=hours_ago(2), company="Mid", priority="medium")
    sec = section(dg.generate_digest(db, now=NOW, with_summary=False), "new_relevant_enquiries")
    assert sec.stats == {"new_relevant": 3, "high_priority": 1, "shown": 3}
    texts = [dg.item_text(i) for i in sec.items]
    assert [t.split(" - ")[0] for t in texts] == ["OnStart", "Mid", "OnEnd"]  # priority high, medium, low
    assert texts[0].endswith("[high]")


def test_top_enquiries_is_capped_but_total_counts_all(db: Session) -> None:
    for i in range(7):
        add_enquiry(db, added=hours_ago(1 + i / 10), company=f"Co{i}")
    sec = section(dg.generate_digest(db, now=NOW, with_summary=False), "new_relevant_enquiries")
    assert sec.stats["new_relevant"] == 7 and len(sec.items) == dg.TOP_ENQUIRIES


def test_awaiting_review_counts_are_not_windowed(db: Session) -> None:
    add_enquiry(db, added=hours_ago(90), priority="high")  # old but still pending
    add_enquiry(db, added=hours_ago(1), priority="medium")
    add_enquiry(db, added=hours_ago(1), verdict="discard", priority="high")
    add_enquiry(db, added=hours_ago(1), review="accepted")
    sec = section(dg.generate_digest(db, now=NOW, with_summary=False), "awaiting_review")
    assert sec.stats == {"pending_review": 3, "genuine": 2, "high": 1}


def test_outreach_counts_failed_and_stuck(db: Session, team: Team) -> None:
    ann, bob = team.ann, team.bob
    add_message(db, ann, created=hours_ago(2))  # drafted in window
    add_message(db, ann, created=hours_ago(24))  # drafted exactly at the start: in
    add_message(db, ann, created=hours_ago(25))  # drafted before: out
    add_message(db, ann, state="approved", approved_at=hours_ago(3))  # approved in window (created 100h ago)
    add_message(db, bob, state="approved", approved_at=hours_ago(30))  # approved before the window
    add_message(db, ann, state="sent", sent_at=hours_ago(4), approved_at=hours_ago(5))
    add_message(db, bob, state="failed", updated_at=hours_ago(6), error="Gmail said no\n" + "x" * 300, subject="Bob mail")
    add_message(db, bob, state="failed", updated_at=hours_ago(40), error="old failure")
    add_message(db, ann, state="sending", updated_at=NOW - timedelta(minutes=10), subject="Stuck one")
    add_message(db, ann, state="sending", updated_at=NOW - timedelta(minutes=1), subject="Fresh send")
    sec = section(dg.generate_digest(db, now=NOW, with_summary=False), "outreach")
    assert sec.stats == {"drafted": 2, "approved": 2, "sent": 1, "failed": 1, "stuck_sending": 1}
    texts = [dg.item_text(i) for i in sec.items]
    failed = next(t for t in texts if t.startswith("FAILED"))
    assert "Bob Das" in failed and "Bob mail" in failed and "Gmail said no" in failed and len(failed) < 260
    assert any("STUCK" in t and "Stuck one" in t and "Ann Rao" in t for t in texts)
    assert not any("Fresh send" in t for t in texts)


def test_scrape_health_reports_runs_cooldown_and_session(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "120")
    reset_settings()
    add_run(db, started=hours_ago(30), posts_new=99)  # outside
    add_run(db, started=hours_ago(10), posts_new=4, errors=1)
    aborted = add_run(db, started=hours_ago(1), status="aborted", abort="rate_limited", posts_new=2, errors=2)
    sec = section(dg.generate_digest(db, now=NOW, with_summary=False), "scrape_health")
    assert sec.stats["runs"] == 2 and sec.stats["posts_new"] == 6 and sec.stats["errors"] == 3 and sec.stats["aborted"] == 1
    assert sec.stats["cooldown_active"] is True
    texts = [dg.item_text(i) for i in sec.items]
    assert any(f"Run #{aborted.id} aborted" in t and "stopped: rate_limited" in t for t in texts)
    assert any("cool-down" in t for t in texts)


def test_cost_today_section(db: Session) -> None:
    add_llm_call(db, micros=12_300, at=hours_ago(1))
    add_llm_call(db, micros=700, at=hours_ago(2))
    sec = section(dg.generate_digest(db, now=NOW, with_summary=False), "cost_today")
    assert sec.stats["today_usd"] == "$0.0130" and sec.stats["today_calls"] == 2


def test_a_failing_section_is_isolated(db: Session) -> None:
    def boom(_db: Session, _w: dg.Window) -> dg.SectionResult:
        raise RuntimeError("kaboom")

    dg.register_section(dg.DigestSection("zz_boom", "Broken", 5, boom))
    try:
        view = dg.generate_digest(db, now=NOW, with_summary=False)
    finally:
        dg.unregister_section("zz_boom")
    assert [s.key for s in view.sections][0] == "zz_boom"
    bad = section(view, "zz_boom")
    assert bad.error and "kaboom" in bad.error and "could not be built" in dg.item_text(bad.items[0])
    assert section(view, "cost_today").error is None  # the rest still built
    assert "BROKEN" in dg.digest_text(view)


def test_rebuilding_the_same_date_replaces_the_row(db: Session, team: Team) -> None:
    first = dg.generate_digest(db, now=NOW, with_summary=False)
    db.get(Digest, first.id).gmail_draft_id = "d-1"  # type: ignore[union-attr]
    db.commit()
    add_enquiry(db, added=hours_ago(1))
    second = dg.generate_digest(db, now=NOW + timedelta(minutes=30), trigger="scheduled", with_summary=False)
    assert db.scalar(select(func.count(Digest.id))) == 1
    assert second.id == first.id and second.trigger == "scheduled"
    assert second.gmail_draft_id is None and second.gmail_draft_member_id is None
    assert section(second, "new_relevant_enquiries").stats["new_relevant"] == 1
    # a different local date makes a second row (NOW is 17:30 IST, +7h is the next IST day)
    dg.generate_digest(db, now=NOW + timedelta(hours=7), with_summary=False)
    assert db.scalar(select(func.count(Digest.id))) == 2
    assert [d.digest_date.isoformat() for d in dg.list_digests(db)] == ["2026-10-29", "2026-10-28"]
    assert dg.latest_digest(db).digest_date.isoformat() == "2026-10-29"  # type: ignore[union-attr]


def test_bad_trigger_is_rejected(db: Session) -> None:
    with pytest.raises(dg.DomainError):
        dg.generate_digest(db, trigger="weird", now=NOW)


# ---------------------------------------------------------------------- summary (fake LLM)
def _seed_data(db: Session) -> None:
    add_enquiry(db, added=hours_ago(1), company="Acme Logistics")
    add_enquiry(db, added=hours_ago(2), company="Beta Corp")


def test_summary_is_stored_with_an_llm_call_row(db: Session) -> None:
    _seed_data(db)
    fake = FakeLLMClient(responder=lambda req: {"lines": ["2 new relevant enquiries arrived.", "2 are waiting for review."]})
    view = dg.generate_digest(db, now=NOW, llm=fake, with_summary=True)
    assert view.summary_text == "2 new relevant enquiries arrived.\n2 are waiting for review."
    assert fake.calls == 1 and fake.requests[0].feature == "suggest" and fake.requests[0].prompt_version == "digest-v1"
    assert "Acme Logistics" in fake.requests[0].messages[0]["content"]
    row = db.scalars(select(LlmCall).where(LlmCall.feature == "suggest")).one()
    assert row.status == "ok" and row.cost_usd_micros > 0 and view.summary_cost_usd_micros == row.cost_usd_micros


def test_summary_is_trimmed_to_five_lines(db: Session) -> None:
    fake = FakeLLMClient(responder=lambda req: {"lines": [f"Quiet line {'x' * i}" for i in range(8)]})
    view = dg.generate_digest(db, now=NOW, llm=fake, with_summary=True)
    assert view.summary_text is not None and len(view.summary_text.splitlines()) == 5


def test_summary_with_an_invented_number_is_dropped_and_flagged(db: Session) -> None:
    _seed_data(db)
    fake = FakeLLMClient(responder=lambda req: {"lines": ["There are 9317 new enquiries."]})
    view = dg.generate_digest(db, now=NOW, llm=fake, with_summary=True)
    assert view.summary_text is None
    assert any("9317" in n for n in view.notes)
    assert view.summary_cost_usd_micros > 0  # the call was paid for, so it is recorded


def test_llm_failure_does_not_fail_the_digest(db: Session) -> None:
    _seed_data(db)
    fake = FakeLLMClient([LLMTransientError("overloaded")])
    view = dg.generate_digest(db, now=NOW, llm=fake, with_summary=True)
    assert view.summary_text is None and any("unavailable" in n for n in view.notes)
    assert section(view, "new_relevant_enquiries").stats["new_relevant"] == 2
    row = db.scalars(select(LlmCall).where(LlmCall.feature == "suggest")).one()
    assert row.status == "error" and row.error_type == "transient"


def test_spend_cap_exceeded_makes_no_llm_call_but_stores_the_digest(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "1")
    reset_settings()
    add_llm_call(db, micros=2_000_000, at=hours_ago(1))
    fake = FakeLLMClient(responder=lambda req: {"lines": ["never"]})
    view = dg.generate_digest(db, now=NOW, llm=fake, with_summary=True)
    assert fake.calls == 0 and view.summary_text is None
    assert any("spend cap" in n for n in view.notes)
    assert db.scalar(select(func.count(Digest.id))) == 1


def test_summary_setting_off_or_with_summary_false_makes_no_call(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLLMClient(responder=lambda req: {"lines": ["x"]})
    dg.generate_digest(db, now=NOW, llm=fake, with_summary=False)
    monkeypatch.setenv("SALES0_DIGEST_AI_SUMMARY", "false")
    reset_settings()
    assert get_settings().digest_ai_summary is False
    dg.generate_digest(db, now=NOW, llm=fake)
    assert fake.calls == 0


def test_no_api_key_skips_the_summary_without_error(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    keyless = get_settings().model_copy(update={"anthropic_api_key": None})
    monkeypatch.setattr(dg, "get_settings", lambda: keyless)
    assert keyless.api_key_present is False
    view = dg.generate_digest(db, now=NOW, with_summary=True)
    assert view.summary_text is None and any("key" in n for n in view.notes)


def test_get_digest_unknown_id_is_not_found(db: Session) -> None:
    with pytest.raises(dg.DomainError) as exc:
        dg.get_digest(db, 999)
    assert exc.value.http_status == 404

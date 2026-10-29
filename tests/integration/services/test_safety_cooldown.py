"""Safety cool-down after a LinkedIn safety stop (architecture.md data flow step 6c):
rate_limited / captcha -> SALES0_SAFETY_COOLDOWN_MIN (60), checkpoint / logout ->
SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN (15). Derived from scrape_runs; checked right after
needs_attention; Re-check / Connect never clear it.

Real SQLite file migrated to head; the clock is injected via ``now=``. The stop itself is
produced by the real discovery job (scripted searcher), so the abort row is the one the
product writes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from sales0.clock import ensure_utc, utcnow
from sales0.config import reset_settings
from sales0.db.engine import reset_engine, session_scope
from sales0.db.models import Keyword, ScrapeRun
from sales0.errors import DomainError
from sales0.jobs import check_job, discovery_job
from sales0.scraper.types import SessionCheckResult
from sales0.services import keywords, runs
from sales0.services import session as session_service
from tests.integration.services.helpers import FakeSearcher, post, safety, set_session_status
from tests.support.fake_llm import FakeLLMClient, triage_answer

# 2026-10-27 08:00 UTC = 13:30 Asia/Kolkata.
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
COOLDOWN_PREFIX = "LinkedIn asked us to slow down — next run allowed after "


@pytest.fixture(autouse=True)
def _production_cooldowns(_isolated_runtime: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root conftest turns the cool-down off; these tests exercise the shipped defaults."""
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
    reset_settings()


def add_keywords(*texts: str) -> None:
    with session_scope() as db:
        for t in texts:
            keywords.add_keyword(db, t)


def stop_run(kind: str, trip: str, *texts: str, at: datetime = T0) -> int:
    """Run the real discovery job with a searcher that safety-stops on ``trip``, then pin the
    stop time to ``at`` (the job used the wall clock) and go green again with a Re-check."""
    set_session_status("connected")
    add_keywords(*texts)
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=at - timedelta(minutes=1))
        db.add(run)
        db.flush()
        rid = run.id
    searcher = FakeSearcher({t: [post(i, f"post {i} for {t}")] for i, t in enumerate(texts, 1)} | {trip: safety(kind)})
    discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()),
                          searcher_factory=searcher, sleep=lambda _s: None)
    with session_scope() as db:
        run = db.get(ScrapeRun, rid)
        assert run is not None and (run.status, run.abort_reason) == ("aborted", kind)
        run.finished_at = at
    recheck_green()
    return rid


def recheck_green() -> None:
    check_job.execute(lambda *_a, **_k: SessionCheckResult(logged_in=True, reason="ok", url="http://x/feed/"))
    with session_scope() as db:
        assert session_service.session_status(db).stored_status == "connected"


def eligibility(now: datetime):
    with session_scope() as db:
        return runs.run_eligibility(db, now=now)


def test_rate_limit_stop_blocks_run_now_for_60_min_after_a_green_recheck_then_allows(db_url: str) -> None:
    stop_run("rate_limited", "ratelimit-test", "ratelimit-test", "ai workshop")

    blocked = eligibility(T0 + timedelta(minutes=59))
    assert (blocked.can_run, blocked.reason_code) == (False, "safety_cooldown")
    # 08:00 UTC + 60 min = 09:00 UTC = 14:30 Asia/Kolkata.
    assert blocked.reason == "LinkedIn asked us to slow down — next run allowed after 14:30"
    with session_scope() as db:
        assert runs.safety_cooldown_until(db, now=T0 + timedelta(minutes=59)) == T0 + timedelta(minutes=60)

    allowed = eligibility(T0 + timedelta(minutes=61))
    assert (allowed.can_run, allowed.reason_code, allowed.reason) == (True, None, None)


def test_captcha_stop_uses_the_long_cooldown(db_url: str) -> None:
    stop_run("captcha", "captcha-test", "captcha-test")
    assert eligibility(T0 + timedelta(minutes=45)).reason_code == "safety_cooldown"
    assert eligibility(T0 + timedelta(minutes=61)).can_run is True


@pytest.mark.parametrize("kind", ["checkpoint", "logout"])
def test_checkpoint_and_logout_stops_have_the_15_min_cooldown(db_url: str, kind: str) -> None:
    stop_run(kind, "checkpoint-test", "checkpoint-test")
    blocked = eligibility(T0 + timedelta(minutes=14))
    assert blocked.reason_code == "safety_cooldown"
    assert blocked.reason == "LinkedIn asked us to slow down — next run allowed after 13:45"
    assert eligibility(T0 + timedelta(minutes=16)).can_run is True


def test_start_run_is_refused_with_429_during_the_cooldown_and_queues_nothing(db_url: str) -> None:
    stop_run("captcha", "captcha-test", "captcha-test", at=utcnow())
    with session_scope() as db, pytest.raises(DomainError) as exc_info:
        runs.start_run(db)
    err = exc_info.value
    assert (err.code, err.http_status) == ("safety_cooldown", 429)
    assert err.message.startswith(COOLDOWN_PREFIX)
    with session_scope() as db:
        assert db.scalar(select(func.count(ScrapeRun.id))) == 1  # only the stopped run


def test_needs_attention_comes_before_the_cooldown_in_the_eligibility_order(db_url: str) -> None:
    stop_run("captcha", "captcha-test", "captcha-test")
    with session_scope() as db:
        session_service.mark_needs_attention(db, "captcha", "https://www.linkedin.com/checkpoint/challenge/x", now=T0)
    assert eligibility(T0 + timedelta(minutes=5)).reason_code == "needs_attention"
    recheck_green()
    assert eligibility(T0 + timedelta(minutes=6)).reason_code == "safety_cooldown"


def test_cooldown_comes_before_not_connected(db_url: str) -> None:
    stop_run("rate_limited", "ratelimit-test", "ratelimit-test")
    set_session_status("not_connected")
    assert eligibility(T0 + timedelta(minutes=5)).reason_code == "safety_cooldown"


def test_zero_disables_the_cooldown_and_each_setting_applies_to_its_kinds(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "0")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "0")
    reset_settings()
    stop_run("captcha", "captcha-test", "captcha-test")
    assert eligibility(T0 + timedelta(seconds=10)).can_run is True
    with session_scope() as db:
        assert runs.safety_cooldown_until(db, now=T0) is None

    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "30")  # does not apply to captcha
    reset_settings()
    assert eligibility(T0 + timedelta(seconds=10)).can_run is True
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "5")
    reset_settings()
    assert eligibility(T0 + timedelta(minutes=4)).reason_code == "safety_cooldown"
    assert eligibility(T0 + timedelta(minutes=5, seconds=1)).can_run is True


def test_no_cooldown_without_a_safety_stop(db_url: str) -> None:
    set_session_status("connected")
    add_keywords("ai workshop")
    with session_scope() as db:
        db.add_all([
            ScrapeRun(trigger="manual", mode="live", status="succeeded", started_at=T0, finished_at=T0,
                      abort_reason="daily_post_cap"),
            ScrapeRun(trigger="manual", mode="live", status="failed", started_at=T0, finished_at=T0,
                      error_message="Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"),
        ])
    with session_scope() as db:
        assert runs.safety_cooldown_until(db, now=T0 + timedelta(minutes=1)) is None
    assert eligibility(T0 + timedelta(minutes=1)).can_run is True


def test_the_latest_stop_by_finish_time_decides(db_url: str) -> None:
    """An older long (captcha) stop that finished earlier does not outlive a newer one."""
    set_session_status("connected")
    add_keywords("ai workshop")
    with session_scope() as db:
        db.add_all([
            ScrapeRun(trigger="manual", mode="live", status="aborted", abort_reason="checkpoint",
                      started_at=T0, finished_at=T0 + timedelta(minutes=2)),
            ScrapeRun(trigger="manual", mode="live", status="aborted", abort_reason="captcha",
                      started_at=T0 - timedelta(hours=3), finished_at=T0 - timedelta(hours=3)),
        ])
    with session_scope() as db:
        assert runs.safety_cooldown_until(db, now=T0) == T0 + timedelta(minutes=17)


def test_cooldown_shown_after_midnight_local_reads_tomorrow(db_url: str) -> None:
    late = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)  # 23:30 IST; +60 min = 00:30 IST next day
    stop_run("rate_limited", "ratelimit-test", "ratelimit-test", at=late)
    assert eligibility(late + timedelta(minutes=1)).reason == (
        "LinkedIn asked us to slow down — next run allowed after 00:30 tomorrow")


def test_session_status_view_exposes_cooldown_until_and_it_survives_a_restart(db_url: str) -> None:
    stop_run("captcha", "captcha-test", "captcha-test", at=utcnow())
    reset_engine()  # state survival: derived purely from the stored run
    with session_scope() as db:
        view = session_service.session_status(db)
    assert view.stored_status == "connected"
    assert view.cooldown_until is not None
    assert view.cooldown_until > utcnow() + timedelta(minutes=55)


# ---------------------------------------------------------------------- keyword stamping
def test_the_keyword_running_at_the_stop_is_stamped_and_sorts_last(db_url: str) -> None:
    before = utcnow()
    stop_run("rate_limited", "ratelimit-test", "ratelimit-test", "ai workshop", "ai consulting")
    with session_scope() as db:
        stamped = {k.text: k.last_run_at for k in db.scalars(select(Keyword))}
        run = db.scalars(select(ScrapeRun)).one()
        assert stamped["ratelimit-test"] is not None and ensure_utc(stamped["ratelimit-test"]) >= before
        assert stamped["ai workshop"] is None and stamped["ai consulting"] is None  # never searched
        assert run.abort_reason == "rate_limited"
    from sales0.jobs.discovery_job import select_keywords

    with session_scope() as db:
        assert [t for _i, t in select_keywords(db, 10)] == ["ai workshop", "ai consulting", "ratelimit-test"]


def test_a_stop_at_the_run_start_check_stamps_no_keyword(db_url: str) -> None:
    set_session_status("connected")
    add_keywords("ai workshop", "ai consulting")
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=utcnow())
        db.add(run)
        db.flush()
        rid = run.id
    discovery_job.execute(rid, llm=FakeLLMClient(responder=lambda req: triage_answer()),
                          searcher_factory=FakeSearcher({}, enter_error=safety("checkpoint")), sleep=lambda _s: None)
    with session_scope() as db:
        assert db.get(ScrapeRun, rid).abort_reason == "checkpoint"  # type: ignore[union-attr]
        assert set(db.scalars(select(Keyword.last_run_at))) == {None}

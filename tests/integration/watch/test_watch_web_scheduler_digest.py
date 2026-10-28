# ruff: noqa: F811, ANN001
"""Surfaces around the watch: /tracked page + htmx run panel, inbox Track button, scheduler
registration (default disabled, next run, overlap), the digest section, append-to-discovery, migration."""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import get_engine, get_sessionmaker, session_scope
from sales0.db.migrate_check import alembic_config, head_revision
from sales0.db.models import LinkedinProfile, ScheduledJob, ScrapeRun, WatchRun
from sales0.jobs import discovery_job, scheduled_watch, watch_job
from sales0.jobs.runner import get_runner, reset_runner
from sales0.jobs.scheduled_discovery import DiscoveryStart
from sales0.jobs.scheduler import Scheduler, WatchJob
from sales0.llm import set_llm_client
from sales0.services import digest, runs
from sales0.web.app import create_app
from tests.integration.jobs.helpers import FakeSearcher
from tests.integration.services.helpers import post, set_session_status
from tests.integration.watch.helpers import WatchSearcher, add_profile, profile_post
from tests.integration.web.conftest import HX, make_enquiry
from tests.support.fake_llm import FakeLLMClient

IST = timedelta(hours=5, minutes=30)
REQ = "Northwind Logistics wants a corporate trainer for a 2-day Generative AI workshop in Mumbai, 30 managers, December."


@pytest.fixture
def web(db_url: str):
    with TestClient(create_app()) as c:
        yield c
    reset_runner()


# ---------------------------------------------------------------------- migration
def test_migration_adds_watch_columns_and_table_and_is_the_single_head(db_url: str) -> None:
    insp = inspect(get_engine())
    cols = {c["name"] for c in insp.get_columns("linkedin_profiles")}
    assert {"last_checked_at", "watch_checks", "watch_hits", "watch_last_status", "watch_disabled_until"} <= cols
    assert "watch_runs" in insp.get_table_names()
    assert head_revision() == "p6_watch_e5b2a7c4"
    command.downgrade(alembic_config(), "p6_crm_5b2e7d14")
    assert "watch_runs" not in inspect(get_engine()).get_table_names()
    command.upgrade(alembic_config(), "head")


# ---------------------------------------------------------------------- /tracked page
def test_tracked_page_lists_profiles_and_toggles(web: TestClient) -> None:
    a = add_profile("in/asha-rao", name="Asha Rao")
    add_profile("in/untracked", tracked=False)
    html = web.get("/tracked").text
    assert 'id="tracked-table"' in html and "Asha Rao" in html and "untracked" not in html.lower().split("profiles (")[1]
    assert "Run watch now" in html and "1 profile due now" in html
    nav = web.get("/inbox").text
    assert 'href="/tracked"' in nav

    r = web.post(f"/tracked/{a}/pause", data={"paused": "1"}, follow_redirects=False)
    assert r.status_code == 303
    html = web.get("/tracked").text
    assert 'data-paused="1"' in html and "Paused" in html and "0 profiles due now" in html
    assert "disabled" in re.search(r'<button id="watch-run-now"[^>]*>', html).group(0)
    web.post(f"/tracked/{a}/pause", data={"paused": "0"})
    web.post(f"/tracked/{a}/untrack")
    assert 'id="tracked-empty"' in web.get("/tracked").text
    web.post(f"/tracked/{a}/track")
    assert 'data-slug="in/asha-rao"' in web.get("/tracked").text


def test_run_watch_now_htmx_shows_progress_then_the_log(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    add_profile("in/asha-rao")
    set_session_status("connected")
    set_llm_client(FakeLLMClient.with_oracle(lambda t: "genuine"))
    monkeypatch.setattr(watch_job, "_default_factory", lambda _s: WatchSearcher({"in/asha-rao": [profile_post("in/asha-rao", "k", REQ)]}))
    r = web.post("/tracked/run", headers=HX)
    assert r.status_code == 200 and 'id="watch-panel"' in r.text
    assert get_runner().wait(60)
    run_id = int(re.search(r'data-run-id="(\d+)"', r.text).group(1))
    done = web.get(f"/tracked/runs/{run_id}/progress", headers=HX)
    assert 'data-status="succeeded"' in done.text and 'hx-trigger' not in done.text
    assert 'id="watch-log"' in done.text and 'data-slug="in/asha-rao"' in done.text
    assert "relevant 1" in done.text
    page = web.get("/tracked").text
    assert 'id="watch-log"' in page and "hits 1" in page
    assert 'data-reason-code="nothing_due"' in page  # just checked: nothing due now
    # the same run shows on the generic Runs page too
    assert f"/runs/{run_id}" in web.get("/runs").text


def test_run_watch_now_is_refused_while_a_discovery_run_is_active(web: TestClient) -> None:
    add_profile("in/asha-rao")
    set_session_status("connected")
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="running", started_at=utcnow()))
    page = web.get("/tracked").text
    assert 'data-reason-code="run_in_progress"' in page
    assert "already in progress" in web.post("/tracked/run", headers=HX).text


def test_inbox_card_has_a_track_button_that_toggles(web: TestClient) -> None:
    pid = add_profile("in/asha-rao", tracked=None)
    eid = make_enquiry("P1", triage_status="done", ai_verdict="genuine", review_status="pending_review", author_profile_id=pid)
    make_enquiry("P2", triage_status="done", ai_verdict="genuine", review_status="pending_review")  # no author profile: no button
    html = web.get("/inbox").text
    assert html.count('data-action="track"') == 1
    r = web.post(f"/tracked/enquiry/{eid}/track", headers=HX)
    assert 'data-tracked="1"' in r.text and "Untrack" in r.text
    with session_scope() as db:
        assert db.get(LinkedinProfile, pid).tracked is True
    r = web.post(f"/tracked/enquiry/{eid}/untrack", headers=HX)
    assert 'data-action="track"' in r.text
    with session_scope() as db:
        assert db.get(LinkedinProfile, pid).tracked is False
    other = make_enquiry("P3", triage_status="done", ai_verdict="genuine", review_status="pending_review")
    assert "no author profile" in web.post(f"/tracked/enquiry/{other}/track", headers=HX).text


# ---------------------------------------------------------------------- scheduler
class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def ist(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC) - IST


def sched_row(name: str) -> ScheduledJob:
    with session_scope() as db:
        r = db.scalar(select(ScheduledJob).where(ScheduledJob.name == name))
        assert r is not None
        db.expunge(r)
        return r


def test_profile_watch_is_registered_default_disabled_and_shows_a_next_run_once_enabled(db_url: str) -> None:
    clock = Clock(ist(28, 12))
    s = Scheduler(sessions=get_sessionmaker(), now=clock, rng=random.Random(1))
    assert "profile_watch" in s.defs and isinstance(s.defs["profile_watch"], WatchJob)
    s.ensure_rows()
    assert sched_row("profile_watch").enabled is False and sched_row("profile_watch").next_run_at is None
    view = {v.name: v for v in s.snapshot()}["profile_watch"]
    assert not view.enabled and view.next_run_at is None and "25 profiles" in view.schedule_text
    s.set_enabled("profile_watch", True)
    assert sched_row("profile_watch").next_run_at == ist(29, 5, 30)  # today's 05:30 passed -> tomorrow
    assert {v.name: v for v in s.snapshot()}["profile_watch"].next_run_at == ist(29, 5, 30)


def test_scheduled_watch_fires_when_due_records_the_run_and_never_overlaps(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    add_profile("in/asha-rao")
    set_session_status("connected")
    started: list[int] = []
    monkeypatch.setattr(scheduled_watch, "start_scheduled_watch", lambda: (started.append(1), DiscoveryStart(True, run_id=9))[1])
    clock = Clock(ist(28, 12))
    s = Scheduler(sessions=get_sessionmaker(), now=clock, rng=random.Random(1), jobs=[WatchJob()])
    s.set_enabled("profile_watch", True)
    assert s.tick() == []
    clock.now = ist(29, 5, 31)
    assert s.tick() == ["profile_watch"] and started == [1]
    r = sched_row("profile_watch")
    assert (r.last_status, r.last_run_id) == ("ok", 9) and r.next_run_at == ist(30, 5, 30)


def test_real_scheduled_start_skips_with_a_reason_during_a_discovery_run(db_url: str) -> None:
    add_profile("in/asha-rao")
    set_session_status("connected")
    with session_scope() as db:
        db.add(ScrapeRun(trigger="manual", mode="live", status="running", started_at=utcnow()))
    out = scheduled_watch.start_scheduled_watch()
    assert not out.started and out.code == "run_in_progress" and out.retryable
    s = Scheduler(sessions=get_sessionmaker(), now=Clock(ist(29, 6)), rng=random.Random(1), jobs=[WatchJob()])
    res = s.run_now("profile_watch")
    assert res.status == "skipped" and "already in progress" in res.detail


def test_nothing_due_is_an_ok_result_not_a_failure(db_url: str) -> None:
    set_session_status("connected")
    s = Scheduler(sessions=get_sessionmaker(), now=Clock(ist(29, 6)), rng=random.Random(1), jobs=[WatchJob()])
    res = s.run_now("profile_watch")
    assert res.status == "ok" and "No tracked profile is due" in res.detail


# ---------------------------------------------------------------------- digest
def test_digest_section_lists_tracked_profile_hits(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "tracked_profile_hits" in [s.key for s in digest.sections()]
    add_profile("in/asha-rao", name="Asha Rao")
    set_session_status("connected")
    monkeypatch.setattr(watch_job.time, "sleep", lambda _s: None)
    with session_scope() as db:
        rid = ScrapeRun(trigger="manual", mode="live", status="queued", started_at=utcnow())
        db.add(rid)
        db.flush()
        db.add(WatchRun(run_id=rid.id))
        run_id = rid.id
    watch_job.execute(run_id, llm=FakeLLMClient.with_oracle(lambda t: "genuine"),
                      searcher_factory=WatchSearcher({"in/asha-rao": [profile_post("in/asha-rao", "k", REQ)]}), sleep=lambda _s: None)
    now = utcnow()
    section = next(s for s in digest.sections() if s.key == "tracked_profile_hits")
    with session_scope() as db:
        res = section.build(db, digest.Window(now - timedelta(hours=24), now + timedelta(minutes=1)))
        assert res.stats == {"hits": 1, "profiles": 1}
        assert "Priya Menon posted a new requirement" in digest.item_text(res.items[0])
        empty = section.build(db, digest.Window(now - timedelta(days=5), now - timedelta(days=4)))
        assert empty.items == [] and "tracked profiles" in empty.empty_text


# ---------------------------------------------------------------------- append to discovery
def test_watch_visits_append_to_a_discovery_run_only_when_the_flag_is_on(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.services import keywords

    for flag in ("false", "true"):
        monkeypatch.setenv("SALES0_WATCH_APPEND_TO_DISCOVERY", flag)
        monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MIN_S", "0")
        monkeypatch.setenv("SALES0_WATCH_PROFILE_PAUSE_MAX_S", "0")
        reset_settings()
        with session_scope() as db:
            db.query(LinkedinProfile).delete()
            db.query(ScrapeRun).delete()
        set_session_status("connected")
        with session_scope() as db:
            if flag == "false":
                keywords.add_keyword(db, "ai workshop")
        add_profile("in/asha-rao")

        class Both(FakeSearcher):
            def scan_profile(self, slug, max_posts):
                return WatchSearcher({"in/asha-rao": [profile_post("in/asha-rao", "k" + flag, REQ + flag)]}).scan_profile(slug, max_posts)

        searcher = Both({"ai workshop": [post(5, "Zentrix Retail wants an AI agents workshop for 30 engineers in Bengaluru")]})
        with session_scope() as db:
            run = ScrapeRun(trigger="manual", mode="live", status="queued", started_at=utcnow())
            db.add(run)
            db.flush()
            rid = run.id
        discovery_job.execute(rid, llm=FakeLLMClient.with_oracle(lambda t: "genuine"), searcher_factory=searcher, sleep=lambda _s: None)
        with session_scope() as db:
            d = runs.get_run(db, rid)
            assert d.run.status == "succeeded"
            visited = [k.keyword_text for k in d.keywords]
            if flag == "false":
                assert visited == ["ai workshop"]
            else:
                assert visited == ["ai workshop", "watch:in/asha-rao"]
                assert (d.run.keywords_planned, d.run.keywords_done) == (2, 2)
                assert db.scalar(select(LinkedinProfile.watch_checks)) == 1

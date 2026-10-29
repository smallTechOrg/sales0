"""plan_keywords (resume-first), recover_interrupted_runs and the Phase 1.4 run view fields."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.db.models import Keyword, ScrapeRun, ScrapeRunKeyword
from sales0.services import keywords, runs

T0 = utcnow() - timedelta(days=1)


def _kw(db: Session, text: str, *, last_run_at=None, active: bool = True) -> int:
    view = keywords.add_keyword(db, text)
    row = db.get(Keyword, view.id)
    assert row is not None
    row.last_run_at, row.active = last_run_at, active
    db.flush()
    return view.id


def _run(db: Session, status: str, rows: dict[int, str], started=T0) -> int:
    run = ScrapeRun(trigger="manual", mode="live", status=status, started_at=started, finished_at=started)
    db.add(run)
    db.flush()
    for kid, kstatus in rows.items():
        db.add(ScrapeRunKeyword(run_id=run.id, keyword_id=kid, keyword_text=f"k{kid}", status=kstatus))
    db.flush()
    return run.id


def test_plan_orders_never_run_first_then_least_recently_run_then_id_and_skips_inactive(db: Session) -> None:
    old = _kw(db, "old", last_run_at=T0 - timedelta(days=5))
    recent = _kw(db, "recent", last_run_at=T0)
    never_b = _kw(db, "never b")
    never_a = _kw(db, "never a")
    _kw(db, "off", active=False)
    assert [i for i, _ in runs.plan_keywords(db, 10)] == [never_b, never_a, old, recent]
    assert runs.plan_keywords(db, 2) == [(never_b, "never b"), (never_a, "never a")]
    assert runs.plan_keywords(db, 0) == []


def test_plan_puts_the_unreached_keywords_of_the_latest_aborted_run_first(db: Session) -> None:
    done = _kw(db, "done kw", last_run_at=T0 + timedelta(minutes=5))
    unreached = _kw(db, "unreached", last_run_at=T0 - timedelta(days=9))
    running = _kw(db, "was running", last_run_at=T0 - timedelta(days=8))
    skipped = _kw(db, "skipped", last_run_at=T0 - timedelta(days=7))
    never = _kw(db, "never run")
    _run(db, "aborted", {done: "done", unreached: "pending", running: "running", skipped: "skipped"})
    # the run being started right now (queued, no keyword rows yet) must not hide the resume set
    _run(db, "queued", {}, started=utcnow())
    plan = [i for i, _ in runs.plan_keywords(db, 10)]
    assert plan[:3] == [unreached, running, skipped]
    assert plan[3:] == [never, done]


def test_plan_does_not_resume_a_keyword_that_already_ran_after_that_run_started(db: Session) -> None:
    ran_since = _kw(db, "ran since", last_run_at=utcnow())
    other = _kw(db, "other", last_run_at=T0 - timedelta(days=3))
    _run(db, "aborted", {ran_since: "skipped"})
    assert [i for i, _ in runs.plan_keywords(db, 10)] == [other, ran_since]


def test_plan_after_a_clean_run_is_plain_least_recently_run_and_ignores_deleted_keywords(db: Session) -> None:
    a = _kw(db, "kw a", last_run_at=T0 - timedelta(days=2))
    b = _kw(db, "kw b", last_run_at=T0 - timedelta(days=4))
    gone = _kw(db, "gone", last_run_at=T0 - timedelta(days=9))
    _run(db, "succeeded", {a: "done", b: "done", gone: "skipped"})
    keywords.remove_keyword(db, gone)
    assert [i for i, _ in runs.plan_keywords(db, 5)] == [b, a]


def test_recover_interrupted_runs_aborts_without_a_reason_and_skips_unfinished_keywords(db: Session) -> None:
    kid = _kw(db, "kw")
    running = _run(db, "running", {kid: "running"})
    finished = _run(db, "succeeded", {kid: "done"})
    assert runs.recover_interrupted_runs(db) == 1
    db.expire_all()
    run = db.get(ScrapeRun, running)
    assert run is not None
    assert (run.status, run.abort_reason, run.error_message) == ("aborted", None, runs.INTERRUPTED_MESSAGE)
    assert "next run continues with the keywords it had not reached" in (run.error_message or "")
    row = db.query(ScrapeRunKeyword).filter_by(run_id=running).one()
    assert row.status == "skipped"
    assert db.get(ScrapeRun, finished).status == "succeeded"  # type: ignore[union-attr]
    assert runs.safety_cooldown_until(db) is None  # no cool-down for an interrupted run
    assert runs.recover_interrupted_runs(db) == 0


def test_run_and_keyword_views_expose_the_phase_1_4_counters_and_relevance_pct(db: Session) -> None:
    kid = _kw(db, "kw")
    rid = _run(db, "succeeded", {kid: "done"})
    run = db.get(ScrapeRun, rid)
    assert run is not None
    run.posts_seen, run.posts_new, run.posts_relevant, run.posts_skipped_ai, run.permalinks_found = 312, 140, 9, 131, 6
    row = db.query(ScrapeRunKeyword).filter_by(run_id=rid).one()
    row.posts_relevant, row.posts_skipped_ai, row.permalinks_found, row.scrolls, row.cost_usd_micros = 3, 20, 2, 11, 4500
    db.flush()
    detail = runs.get_run(db, rid)
    assert (detail.run.posts_relevant, detail.run.posts_skipped_ai, detail.run.permalinks_found) == (9, 131, 6)
    assert detail.run.relevance_pct == 6.4
    kv = detail.keywords[0]
    assert (kv.posts_relevant, kv.posts_skipped_ai, kv.permalinks_found, kv.scrolls, kv.cost_usd_micros) == (3, 20, 2, 11, 4500)
    assert runs.list_runs(db)[0].relevance_pct == 6.4


def test_relevance_pct_is_none_when_nothing_was_new(db: Session) -> None:
    rid = _run(db, "succeeded", {})
    assert runs.get_run(db, rid).run.relevance_pct is None

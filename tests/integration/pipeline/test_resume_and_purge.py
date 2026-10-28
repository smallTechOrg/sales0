"""Phase 1.4 gate assertions 8 and 9 on the real job + headless Chromium + bulk fixture:
purge at run start, and a restart mid-run -> ``aborted`` -> the next run continues with the
keywords it had not reached (re-triaging nothing)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from structlog.testing import capture_logs

from sales0.config import Settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, LlmCall, ScrapeRun, SeenPost
from sales0.errors import DomainError
from sales0.services import runs, seen_posts
from sales0.services.session import session_status
from tests.integration.pipeline.helpers import (
    BULK_KEYWORDS,
    Killed,
    Recorder,
    add_bulk_keywords,
    oracle_llm,
    run_discovery,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(480)]


def test_a_restart_mid_run_aborts_it_and_the_next_run_continues_with_the_unreached_keywords(depth: Settings) -> None:
    five = BULK_KEYWORDS[:5]
    add_bulk_keywords(five)
    rec = Recorder()
    llm = oracle_llm(rec)
    rec.kill_on_scan = 3  # the process "dies" as the 3rd keyword starts

    with pytest.raises(Killed):
        run_discovery(rec, llm)
    assert rec.scan_order == list(five[:3])
    with session_scope() as db:
        run = db.scalars(select(ScrapeRun)).one()
        assert run.status == "running"  # nothing could mark it: that is what a kill looks like
        rid1 = run.id
        done_seen = db.scalar(select(func.count(SeenPost.id)))
    assert done_seen and done_seen > 0

    # server restart: startup recovery
    with session_scope() as db:
        assert runs.recover_interrupted_runs(db) == 1
    with session_scope() as db:
        d1 = runs.get_run(db, rid1)
        status = session_status(db)
        assert runs.run_eligibility(db).can_run, "an interrupted run starts no cool-down"
    assert d1.run.status == "aborted" and d1.run.abort_reason is None
    assert "Interrupted by server restart" in (d1.run.error_message or "")
    assert [k.status for k in d1.keywords] == ["done", "done", "skipped", "skipped", "skipped"]
    assert status.status == "connected"

    calls_after_run1, seen_after_run1 = llm.calls, done_seen
    rec.kill_on_scan = None
    rec.scan_order.clear()
    rid2 = run_discovery(rec, llm)
    # exactly the 3 unfinished keywords are planned first (resume-first), then the 2 finished ones
    assert [k for k in rec.scan_order][:3] == list(five[2:])
    with session_scope() as db:
        d2 = runs.get_run(db, rid2)
        total_seen = db.scalar(select(func.count(SeenPost.id)))
        triage_rows = db.scalar(select(func.count(LlmCall.id)).where(LlmCall.feature == "triage"))
    assert d2.run.status == "succeeded" and d2.run.keywords_done == 5
    assert [k.keyword_text for k in d2.keywords][:3] == list(five[2:])
    # nothing is triaged twice: every distinct post cost exactly one call over both runs
    assert llm.calls == triage_rows == total_seen > seen_after_run1 >= 1
    assert llm.calls - calls_after_run1 == d2.run.posts_new == total_seen - seen_after_run1


def test_purge_at_run_start_keeps_the_fingerprint_and_a_purged_post_found_again_makes_no_row_and_no_call(
    depth: Settings,
) -> None:
    add_bulk_keywords(BULK_KEYWORDS[:1])
    rec = Recorder()
    llm = oracle_llm(rec)
    run_discovery(rec, llm)
    with session_scope() as db:
        discards = list(db.scalars(select(SeenPost).where(SeenPost.verdict == "discard")))
        assert discards and all(sp.raw_text for sp in discards)
        target_id = discards[0].id
        fingerprints = {sp.id: (sp.fingerprint, sp.snippet) for sp in discards}
        # Rescue works while the text is retained (one post), then expire the rest.
        rescued = seen_posts.rescue(db, discards[-1].id)
        assert rescued.id
        db.execute(
            update(SeenPost)
            .where(SeenPost.verdict == "discard", SeenPost.id != discards[-1].id)
            .values(raw_purge_after=datetime.now(UTC) - timedelta(days=1))
        )
        counts = {
            "seen": db.scalar(select(func.count(SeenPost.id))),
            "enq": db.scalar(select(func.count(Enquiry.id))),
        }
        expired = len(discards) - 1
    calls_before = llm.calls

    with capture_logs() as logs:
        rid2 = run_discovery(rec, llm)  # the purge is the first thing a run does
    assert next(e for e in logs if e["event"] == "seen_posts_purged")["rows"] == expired
    with session_scope() as db:
        r2 = runs.get_run(db, rid2).run
        purged = db.get(SeenPost, target_id)
        assert purged.raw_text is None and purged.raw_purged_at is not None
        assert (purged.fingerprint, purged.snippet) == fingerprints[target_id]
        assert db.scalar(select(func.count(SeenPost.id))) == counts["seen"]
        assert db.scalar(select(func.count(Enquiry.id))) == counts["enq"] + 0
        with pytest.raises(DomainError) as exc:
            seen_posts.rescue(db, target_id)
        assert exc.value.code == "raw_purged"
    # the purged posts were re-found by run 2 (same fixture): counted as seen, never re-triaged
    assert r2.posts_new == 0 and r2.posts_seen > 0 and llm.calls == calls_before

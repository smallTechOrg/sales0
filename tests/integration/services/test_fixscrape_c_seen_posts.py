"""fixscrape slice C: purge never drops untriaged text (#6/#12), NULL purge dates are backfilled
(#11), re-sightings backfill missing time/headline (#2) and upgrade truncated text (#7)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.db.models import SeenPost
from sales0.jobs import purge_job
from sales0.services.seen_posts import persist_triage, purge_expired, register_sighting
from tests.integration.services.helpers import make_run, sdui_post
from tests.integration.services.test_seen_posts_service import AGENCY, KW, failed_outcome, outcome

LONG = (
    "Our company is planning a three day hands-on generative AI workshop for about forty engineers in Pune next "
    "quarter and we are looking for an experienced trainer who has delivered similar programs for enterprises "
    "before. Budget is approved, please share your outline, past client list and commercials. We would also like "
    "a short demo session before we finalise the vendor for the program."
)
TRUNC = LONG[:150] + "… see more"


def sight(db: Session, key: str, text: str, run_id: int, **kw: object):
    return register_sighting(db, sdui_post(key, text, **kw), run_id, None, KW)  # type: ignore[arg-type]


# ---------------------------------------------------------------- #6 / #12: purge keeps untriaged
def test_purge_keeps_pending_and_failed_text_but_purges_done_discards(db: Session) -> None:
    run_id = make_run()
    pend = sight(db, "p", "pending post text that is long enough", run_id, slug="in/pend")
    fail = sight(db, "f", "failed post text that is long enough", run_id, slug="in/fail")
    done = sight(db, "d", AGENCY, run_id, slug="in/done")
    persist_triage(db, fail.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
    persist_triage(db, done.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    n = purge_expired(db, now=utcnow() + timedelta(days=60))
    assert n == 1
    assert db.get(SeenPost, pend.seen_post_id).raw_text is not None  # type: ignore[union-attr]
    assert db.get(SeenPost, fail.seen_post_id).raw_text is not None  # type: ignore[union-attr]
    assert db.get(SeenPost, done.seen_post_id).raw_text is None  # type: ignore[union-attr]


# ---------------------------------------------------------------- #11: backfill + job
def test_null_purge_dates_are_backfilled_then_purged_only_after_the_date(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "n", AGENCY, run_id)
    persist_triage(db, s.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    row = db.get(SeenPost, s.seen_post_id)
    assert row is not None
    row.raw_purge_after = None
    db.flush()
    first = row.first_seen_at
    assert purge_expired(db, now=first + timedelta(days=400)) == 0  # NULL never purged by itself
    res = purge_job.run_purge(db, now=first + timedelta(days=2), retention_days=14)
    assert res.purged == 0 and res.backfilled == 1
    assert row.raw_purge_after == first + timedelta(days=14) and row.raw_text is not None
    again = purge_job.run_purge(db, now=first + timedelta(days=3), retention_days=14)
    assert again.backfilled == 0 and again.purged == 0  # idempotent
    final = purge_job.run_purge(db, now=first + timedelta(days=15), retention_days=14)
    assert final.purged == 1 and row.raw_text is None


def test_purge_job_hook_returns_count_and_keeps_pending(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "h", AGENCY, run_id)
    row = db.get(SeenPost, s.seen_post_id)
    assert row is not None
    row.raw_purge_after = utcnow() - timedelta(days=1)
    db.flush()
    assert purge_job.purge_hook(db, utcnow()) == 0  # still pending
    persist_triage(db, s.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    row.raw_purge_after = utcnow() - timedelta(days=1)
    db.flush()
    assert purge_job.purge_hook(db, utcnow()) == 1


# ---------------------------------------------------------------- #2: backfill time / headline
def test_resighting_backfills_missing_posted_time_and_headline(db: Session) -> None:
    run_id = make_run()
    first = sdui_post("t1", LONG)
    first = replace(first, posted_relative=None, posted_date=None, author_headline=None)
    s1 = register_sighting(db, first, run_id, None, KW)
    row = db.get(SeenPost, s1.seen_post_id)
    assert row is not None and row.posted_relative is None and row.author_headline is None
    register_sighting(db, sdui_post("t1", LONG), run_id, None, KW)
    assert row.posted_relative == "2d" and row.author_headline == "Head of L&D"
    # an existing value is never overwritten
    register_sighting(db, replace(sdui_post("t1", LONG), posted_relative="9w"), run_id, None, KW)
    assert row.posted_relative == "2d"


# ---------------------------------------------------------------- #7: truncated text upgrade
def test_longer_text_upgrades_a_truncated_pending_row(db: Session) -> None:
    run_id = make_run()
    s1 = sight(db, "u1", TRUNC, run_id)
    s2 = sight(db, "u1", LONG, run_id)
    assert s2.seen_post_id == s1.seen_post_id and s2.triage_status == "pending"
    row = db.get(SeenPost, s1.seen_post_id)
    assert row is not None and row.raw_text == LONG
    # the longer text now matches directly (no second row) from another card ref
    s3 = sight(db, "u2", LONG, run_id)
    assert s3.seen_post_id == s1.seen_post_id and s3.kind == "seen"


def test_longer_text_requeues_discarded_and_failed_rows(db: Session) -> None:
    run_id = make_run()
    d = sight(db, "d1", TRUNC, run_id, slug="in/a")
    persist_triage(db, d.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    f = sight(db, "f1", TRUNC, run_id, slug="in/b")
    persist_triage(db, f.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
    sd = sight(db, "d1", LONG, run_id, slug="in/a")
    sf = sight(db, "f1", LONG, run_id, slug="in/b")
    assert sd.triage_status == "pending" and sf.triage_status == "pending"
    row = db.get(SeenPost, d.seen_post_id)
    assert row is not None and row.verdict is None and row.raw_text == LONG and row.raw_purge_after is not None


def test_shorter_or_unrelated_text_never_replaces_stored_text(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "x", LONG, run_id)
    persist_triage(db, s.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    again = sight(db, "x", TRUNC, run_id)
    assert again.triage_status == "done"
    other = sight(db, "x", "something else entirely " * 10, run_id)
    assert other.triage_status == "done"
    row = db.get(SeenPost, s.seen_post_id)
    assert row is not None and row.raw_text == LONG and row.verdict == "discard"


def test_failed_row_is_requeued_on_resighting_with_same_text(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "r", LONG, run_id)
    persist_triage(db, s.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
    assert sight(db, "r", LONG, run_id).triage_status == "pending"


@pytest.fixture(autouse=True)
def _purge_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw text is kept forever by default; these tests exercise the opt-in 14-day purge."""
    from sales0.config import reset_settings

    monkeypatch.setenv("SALES0_SEEN_RAW_RETENTION_DAYS", "14")
    reset_settings()

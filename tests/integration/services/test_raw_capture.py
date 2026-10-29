"""Maximum raw capture: the new seen_posts columns are stored, back-filled on re-sighting, and raw text is kept
forever by default (the purge job only acts when SALES0_SEEN_RAW_RETENTION_DAYS > 0)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.models import SeenPost
from sales0.jobs import purge_job
from sales0.services.seen_posts import register_sighting
from tests.integration.services.helpers import make_run, sdui_post

KW = "corporate ai training"
TEXT = "Acme Logistics wants a training partner for a 2-day GenAI workshop in Pune. Mail training@acme.example"
CARD = {"inner_text": "Priya Menon\n" + TEXT, "reactions": 12, "comments": 3, "reposts": 1, "links": ["https://acme.example"]}


_RUN: list = []


@pytest.fixture(autouse=True)
def _run(db: Session) -> None:
    """Made before the test session writes anything: a second SQLite writer would hit "database is locked"."""
    _RUN[:] = [make_run()]


def sight(db: Session, post):  # type: ignore[no-untyped-def]
    return register_sighting(db, post, _RUN[0], None, KW)


def test_new_post_stores_every_parsed_field_and_the_raw_card(db: Session) -> None:
    post = replace(sdui_post("k1", TEXT), truncated=True, raw_card=CARD)
    row = db.get(SeenPost, sight(db, post).seen_post_id)
    assert row is not None
    assert (row.posted_date, row.truncated, row.link_kind) == (date(2026, 10, 25), True, "profile")
    assert row.search_url == post.search_url and row.raw_card == CARD
    assert row.raw_text == TEXT and row.raw_purge_after is None  # kept forever by default


def test_a_resighting_fills_what_the_first_one_missed_and_clears_truncated(db: Session) -> None:
    first = replace(sdui_post("k1", TEXT), posted_date=None, search_url=None, truncated=True)
    sid = sight(db, first).seen_post_id
    again = replace(sdui_post("k1", TEXT), raw_card=CARD)
    sight(db, again)
    row = db.get(SeenPost, sid)
    assert row is not None
    assert row.posted_date == date(2026, 10, 25) and row.search_url == again.search_url and row.raw_card == CARD
    assert row.truncated is False  # the later sighting had the full text


def test_default_retention_never_purges_and_cancels_old_purge_dates(db: Session) -> None:
    sid = sight(db, sdui_post("k1", TEXT)).seen_post_id
    row = db.get(SeenPost, sid)
    assert row is not None
    row.triage_status, row.raw_purge_after = "done", utcnow() - timedelta(days=1)  # stamped under an old 14-day policy
    db.flush()
    result = purge_job.run_purge(db, now=utcnow() + timedelta(days=400))
    assert result.purged == 0
    db.refresh(row)
    assert row.raw_text == TEXT and row.raw_purge_after is None


def test_a_positive_retention_still_purges(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SEEN_RAW_RETENTION_DAYS", "14")
    reset_settings()
    sid = sight(db, sdui_post("k1", TEXT)).seen_post_id
    row = db.get(SeenPost, sid)
    assert row is not None and row.raw_purge_after is not None
    row.triage_status = "done"
    db.flush()
    assert purge_job.run_purge(db, now=utcnow() + timedelta(days=15)).purged == 1
    db.refresh(row)
    assert row.raw_text is None

"""Phase 1.4 ORM models on a migrated SQLite file: defaults, uniqueness, enum checks, FK rules."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import get_sessionmaker, session_scope
from sales0.db.models import (
    Enquiry,
    Keyword,
    LinkedinProfile,
    LlmCall,
    ScrapeRun,
    ScrapeRunKeyword,
    SeenPost,
    enums,
)


def _profile(slug: str = "in/priya-menon", **kw: object) -> LinkedinProfile:
    kind = "person" if slug.startswith("in/") else "company"
    return LinkedinProfile(
        slug=slug, kind=kind, profile_url=f"https://www.linkedin.com/{slug}/", **kw
    )


def _seen(fp: str = "f" * 64, **kw: object) -> SeenPost:
    values: dict[str, object] = {
        "fingerprint": fp,
        "content_hash": "c" * 64,
        "keyword_text": "kw",
        "snippet": "s",
    }
    values.update(kw)
    return SeenPost(**values)


def _expect_integrity_error(*objs: object) -> None:
    db = get_sessionmaker()()
    try:
        db.add_all(list(objs))
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_profile_defaults(db_url: str) -> None:
    with session_scope() as db:
        p = _profile()
        db.add(p)
        db.flush()
        assert (p.posts_seen, p.relevant_count) == (0, 0)
        assert p.tracked is None and p.display_name is None
        assert p.first_seen_at.tzinfo is not None and p.last_seen_at.tzinfo is not None


def test_profile_slug_is_unique_and_kind_is_checked(db_url: str) -> None:
    with session_scope() as db:
        db.add(_profile())
    _expect_integrity_error(_profile())
    _expect_integrity_error(LinkedinProfile(slug="in/x", kind="robot", profile_url="u"))


def test_seen_post_defaults_and_constraints(db_url: str) -> None:
    with session_scope() as db:
        p = _seen()
        db.add(p)
        db.flush()
        assert p.triage_status == "pending" and p.times_seen == 1
        assert p.verdict is None and p.raw_text is None and p.enquiry_id is None
        assert p.first_seen_at.tzinfo is not None
    _expect_integrity_error(_seen())  # duplicate fingerprint
    _expect_integrity_error(_seen("a" * 64, triage_status="weird"))
    _expect_integrity_error(_seen("b" * 64, verdict="maybe"))


def test_seen_post_source_ref_is_unique_but_nullable_many_times(db_url: str) -> None:
    with session_scope() as db:
        db.add_all([_seen("1" * 64), _seen("2" * 64)])  # two NULL refs are fine
        db.add(_seen("3" * 64, source_ref="urn:li:activity:1"))
    _expect_integrity_error(_seen("4" * 64, source_ref="urn:li:activity:1"))


def test_deleting_a_profile_or_enquiry_nulls_the_seen_post_links(db_url: str) -> None:
    with session_scope() as db:
        profile = _profile()
        enquiry = Enquiry(content_hash="e" * 64, raw_text="text")
        db.add_all([profile, enquiry])
        db.flush()
        post = _seen(profile_id=profile.id, enquiry_id=enquiry.id)
        db.add(post)
        db.flush()
        enquiry.seen_post_id, enquiry.author_profile_id = post.id, profile.id
        db.add(LlmCall(feature="triage", model="m", status="ok", seen_post_id=post.id))
        db.flush()
        post_id = post.id
    with session_scope() as db:
        db.delete(db.get(LinkedinProfile, 1))
        db.flush()
        db.expire_all()
        assert db.get(SeenPost, post_id).profile_id is None
        assert db.get(Enquiry, 1).author_profile_id is None
        db.delete(db.get(Enquiry, 1))
        db.flush()
        db.expire_all()
        assert db.get(SeenPost, post_id).enquiry_id is None
    with session_scope() as db:
        db.delete(db.get(SeenPost, post_id))
        db.flush()
        assert db.scalar(select(LlmCall.seen_post_id)) is None
        assert (
            db.scalar(select(func.count()).select_from(LlmCall)) == 1
        )  # costs survive


def test_keyword_phase_1_4_columns_and_suggested_key_uniqueness(db_url: str) -> None:
    with session_scope() as db:
        k = Keyword(
            text="Claude Code trainer",
            suggested_key="claude-code-trainer",
            group_name="Technical AI training",
        )
        plain = Keyword(text="my own topic")
        other = Keyword(
            text="another own topic"
        )  # several NULL suggested_keys are fine
        db.add_all([k, plain, other])
        db.flush()
        assert (k.scanned_total, k.relevant_total, k.last_scanned, k.last_relevant) == (
            0,
            0,
            0,
            0,
        )
        assert plain.group_name is None and plain.suggested_key is None
    _expect_integrity_error(
        Keyword(text="Different text", suggested_key="claude-code-trainer")
    )


def test_run_and_run_keyword_counters_default_to_zero(db_url: str) -> None:
    with session_scope() as db:
        run = ScrapeRun(mode="fixture")
        db.add(run)
        db.flush()
        rk = ScrapeRunKeyword(run_id=run.id, keyword_text="kw")
        db.add(rk)
        db.flush()
        assert (run.posts_relevant, run.posts_skipped_ai, run.permalinks_found) == (
            0,
            0,
            0,
        )
        assert (
            rk.posts_relevant,
            rk.posts_skipped_ai,
            rk.permalinks_found,
            rk.scrolls,
            rk.cost_usd_micros,
        ) == (0,) * 5


def test_enquiry_posted_relative_roundtrip_and_enums_exported(db_url: str) -> None:
    with session_scope() as db:
        e = Enquiry(content_hash="9" * 64, raw_text="t", posted_relative="3h")
        db.add(e)
        db.flush()
        assert (
            e.posted_relative == "3h"
            and e.author_profile_id is None
            and e.seen_post_id is None
        )
    assert enums.SEEN_TRIAGE_STATUSES == ("pending", "done", "failed")
    assert enums.PROFILE_KINDS == ("person", "company")

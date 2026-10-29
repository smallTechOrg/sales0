"""Suggested-keyword seeding, edit and yield stats on real SQLite."""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.models import AppMeta, Keyword
from sales0.errors import DomainError
from sales0.services import keywords
from sales0.services.keyword_library import SUGGESTED_KEYWORDS


def test_seed_on_an_empty_table_adds_60_across_four_groups_then_adds_0(db: Session) -> None:
    result = keywords.seed_suggested(db)
    assert (result.added, result.already_present) == (60, 0)
    rows = db.scalars(select(Keyword)).all()
    assert len(rows) == 60 and all(r.active and r.suggested_key for r in rows)
    assert sorted(Counter(r.group_name for r in rows).values()) == [15, 15, 15, 15]
    again = keywords.seed_suggested(db)
    assert (again.added, again.already_present) == (0, 60)
    assert db.scalar(select(func.count(Keyword.id))) == 60
    assert all(k.suggested and k.group_name for k in keywords.list_keywords(db))


def test_seed_never_overwrites_edits_duplicates_users_own_text_but_readds_deleted(db: Session) -> None:
    mine = keywords.add_keyword(db, "Corporate AI training")  # a user's own keyword, same text as a suggestion
    keywords.seed_suggested(db)
    assert db.scalar(select(func.count(Keyword.id))) == 60  # no duplicate of the user's own
    own = db.get(Keyword, mine.id)
    assert own is not None and own.suggested_key is None and own.group_name is None

    edited = db.scalar(select(Keyword).where(Keyword.suggested_key == "looking-for-ai-trainer"))
    assert edited is not None
    keywords.update_keyword(db, edited.id, text="need an AI trainer urgently", group_name="Mine")
    keywords.set_keyword_active(db, edited.id, False)
    deleted = db.scalar(select(Keyword).where(Keyword.suggested_key == "ai-training-partner"))
    assert deleted is not None
    keywords.remove_keyword(db, deleted.id)
    db.commit()

    result = keywords.seed_suggested(db)
    assert (result.added, result.already_present) == (1, 59)  # only the deleted one returns
    db.expire_all()
    edited = db.get(Keyword, edited.id)
    assert edited is not None
    assert (edited.text, edited.group_name, edited.active, edited.suggested_key) == (
        "need an AI trainer urgently", "Mine", False, "looking-for-ai-trainer")
    assert db.scalar(select(func.count(Keyword.id)).where(Keyword.suggested_key == "looking-for-ai-trainer")) == 1
    assert db.scalar(select(func.count(Keyword.id)).where(Keyword.suggested_key == "ai-training-partner")) == 1


def test_seed_suggested_once_runs_once_and_honours_the_setting(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest sets SEED_SUGGESTED_KEYWORDS=false: off -> nothing happens, no marker
    assert keywords.seed_suggested_once(db) is None
    assert db.get(AppMeta, "keywords_seeded_v1") is None and db.scalar(select(func.count(Keyword.id))) == 0
    monkeypatch.setenv("SALES0_SEED_SUGGESTED_KEYWORDS", "true")
    reset_settings()
    first = keywords.seed_suggested_once(db)
    assert first is not None and first.added == len(SUGGESTED_KEYWORDS) == 60
    marker = db.get(AppMeta, "keywords_seeded_v1")
    assert marker is not None and marker.value == "1"
    # the user deletes everything; the startup hook must not bring them back
    for k in keywords.list_keywords(db):
        keywords.remove_keyword(db, k.id)
    assert keywords.seed_suggested_once(db) is None
    assert db.scalar(select(func.count(Keyword.id))) == 0


def test_update_keyword_text_group_duplicate_invalid_and_not_found(db: Session) -> None:
    a = keywords.add_keyword(db, "ai workshop")
    keywords.add_keyword(db, "ai consulting")
    edited = keywords.update_keyword(db, a.id, text="  AI   Workshop for managers ")
    assert (edited.text, edited.group_name) == ("AI Workshop for managers", None)
    grouped = keywords.update_keyword(db, a.id, group_name="Products")
    assert (grouped.text, grouped.group_name) == ("AI Workshop for managers", "Products")  # text kept
    assert keywords.update_keyword(db, a.id, group_name=None).group_name is None
    assert keywords.update_keyword(db, a.id, group_name="Products").group_name == "Products"
    assert keywords.update_keyword(db, a.id, text=None).text == "AI Workshop for managers"  # untouched
    assert keywords.update_keyword(db, a.id, text="AI WORKSHOP FOR MANAGERS").text == "AI WORKSHOP FOR MANAGERS"  # own key
    with pytest.raises(DomainError) as dup:
        keywords.update_keyword(db, a.id, text="AI Consulting")
    assert (dup.value.code, dup.value.http_status) == ("keyword_duplicate", 409)
    with pytest.raises(DomainError) as bad:
        keywords.update_keyword(db, a.id, text="x")
    assert bad.value.code == "keyword_invalid"
    with pytest.raises(DomainError) as nf:
        keywords.update_keyword(db, 9999, text="valid text")
    assert nf.value.code == "not_found"


def test_record_yield_accumulates_totals_and_sets_the_last_run(db: Session) -> None:
    kw = keywords.add_keyword(db, "ai workshop")
    keywords.record_yield(db, kw.id, 80, 3)
    keywords.record_yield(db, kw.id, 60, 0)
    view = next(k for k in keywords.list_keywords(db) if k.id == kw.id)
    assert (view.scanned_total, view.relevant_total, view.last_scanned, view.last_relevant) == (140, 3, 60, 0)


def test_record_yield_on_a_deleted_or_missing_keyword_is_a_no_op(db: Session) -> None:
    kw = keywords.add_keyword(db, "ai workshop")
    keywords.remove_keyword(db, kw.id)
    keywords.record_yield(db, kw.id, 10, 1)
    keywords.record_yield(db, None, 10, 1)
    keywords.record_yield(db, 9999, 10, 1)
    assert keywords.list_keywords(db) == []

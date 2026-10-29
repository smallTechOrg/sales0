"""fixscrape #11: existing DBs receive the v2 phrases exactly once, without dupes or resurrection."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.models import AppMeta, Keyword
from sales0.services import keywords
from sales0.services.keyword_library import ALL_SUGGESTED_KEYWORDS, SUGGESTED_KEYWORDS_V2
from sales0.services.keyword_library_seed import V2_META_KEY, seed_new_phrases


@pytest.fixture(autouse=True)
def _seeding_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_SEED_SUGGESTED_KEYWORDS", "true")
    reset_settings()


def _seed_v1_only(db: Session) -> None:
    keywords.seed_suggested(db)  # the 60 v1 phrases (library v1 only)
    db.add(AppMeta(key=keywords.SEEDED_META_KEY, value="1"))
    db.flush()


def test_existing_seeded_db_gets_v2_once_without_duplicates(db: Session) -> None:
    _seed_v1_only(db)
    assert db.scalar(select(func.count(Keyword.id))) == 60
    added = seed_new_phrases(db)
    assert added == len(SUGGESTED_KEYWORDS_V2)
    assert db.scalar(select(func.count(Keyword.id))) == len(ALL_SUGGESTED_KEYWORDS)
    assert seed_new_phrases(db) == 0 and db.get(AppMeta, V2_META_KEY) is not None


def test_deleted_v2_phrase_and_user_text_are_not_resurrected_or_duplicated(db: Session) -> None:
    _seed_v1_only(db)
    mine = SUGGESTED_KEYWORDS_V2[0].text
    keywords.add_keyword(db, mine.upper())  # the user already has this phrase
    seed_new_phrases(db)
    assert db.scalar(select(func.count(Keyword.id)).where(Keyword.text_key == mine.lower())) == 1
    victim = db.scalar(select(Keyword).where(Keyword.suggested_key == SUGGESTED_KEYWORDS_V2[1].key))
    assert victim is not None
    keywords.remove_keyword(db, victim.id)
    assert seed_new_phrases(db) == 0
    assert db.scalar(select(Keyword.id).where(Keyword.suggested_key == SUGGESTED_KEYWORDS_V2[1].key)) is None


def test_never_seeded_db_or_seeding_disabled_adds_nothing(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    assert seed_new_phrases(db) == 0  # the user never took the suggested set
    _seed_v1_only(db)
    monkeypatch.setenv("SALES0_SEED_SUGGESTED_KEYWORDS", "false")
    reset_settings()
    assert seed_new_phrases(db) == 0

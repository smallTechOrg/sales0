"""Keyword CRUD on real SQLite: normalisation, case-insensitive uniqueness, history kept on delete."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import ScrapeRunKeyword
from sales0.errors import DomainError
from sales0.services import keywords
from tests.integration.services.helpers import make_run


def test_add_keyword_stores_trimmed_collapsed_text_and_rejects_case_duplicates(db: Session) -> None:
    added = keywords.add_keyword(db, "  Corporate   AI training ")
    assert added.text == "Corporate AI training" and added.active and added.last_run_at is None
    with pytest.raises(DomainError) as info:
        keywords.add_keyword(db, "corporate ai training")
    assert info.value.code == "keyword_duplicate" and info.value.http_status == 409
    assert "'Corporate AI training' is already in your keywords." == info.value.message
    assert [k.text for k in keywords.list_keywords(db)] == ["Corporate AI training"]


@pytest.mark.parametrize("text", ["a", " ", "", "x" * 101])
def test_add_keyword_rejects_out_of_range_lengths(db: Session, text: str) -> None:
    with pytest.raises(DomainError) as info:
        keywords.add_keyword(db, text)
    assert info.value.code == "keyword_invalid"
    assert keywords.list_keywords(db) == []


def test_add_keyword_accepts_the_boundary_lengths(db: Session) -> None:
    keywords.add_keyword(db, "ai")
    keywords.add_keyword(db, "y" * 100)
    assert len(keywords.list_keywords(db)) == 2


def test_toggle_and_remove_keyword_keep_run_log_text(db: Session) -> None:
    kw = keywords.add_keyword(db, "ai workshop")
    assert keywords.set_keyword_active(db, kw.id, False).active is False
    db.commit()
    run_id = make_run()
    db.add(ScrapeRunKeyword(run_id=run_id, keyword_id=kw.id, keyword_text=kw.text, status="done"))
    db.commit()
    keywords.remove_keyword(db, kw.id)
    db.commit()
    row = db.scalars(select(ScrapeRunKeyword).where(ScrapeRunKeyword.run_id == run_id)).one()
    assert row.keyword_text == "ai workshop" and row.keyword_id is None
    assert keywords.list_keywords(db) == []


def test_remove_or_toggle_missing_keyword_raises_not_found(db: Session) -> None:
    with pytest.raises(DomainError) as info:
        keywords.remove_keyword(db, 999)
    assert info.value.code == "not_found" and info.value.http_status == 404
    with pytest.raises(DomainError):
        keywords.set_keyword_active(db, 999, True)


def test_keywords_survive_a_new_session(db: Session) -> None:
    keywords.add_keyword(db, "GenAI workshop")
    db.commit()
    from sales0.db.engine import reset_engine, session_scope

    reset_engine()
    with session_scope() as fresh:
        assert [k.text for k in keywords.list_keywords(fresh)] == ["GenAI workshop"]

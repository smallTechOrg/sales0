from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.services.keyword_yield import keyword_yield
from tests.integration.suggest.conftest import Seed, add_enquiry, add_run_stat


def by_text(rows):  # type: ignore[no-untyped-def]
    return {r.text: r for r in rows}


def test_exact_counts(db: Session, seeded: Seed) -> None:
    # extras: a discarded hit and a duplicate hit on the same genuine enquiry must not count
    add_enquiry(
        db,
        "A discarded post about ai workshop",
        verdict="discard",
        hits=[(seeded.kw["ai workshop"], "ai workshop")],
    )
    db.add_all([])
    from sales0.db.models import EnquiryKeywordHit

    db.add(
        EnquiryKeywordHit(
            enquiry_id=seeded.enquiry_ids[0],
            keyword_id=seeded.kw["ai workshop"],
            keyword_text="ai workshop",
        )
    )
    db.commit()
    rows = by_text(keyword_yield(db))
    aw = rows["ai workshop"]
    assert (aw.scanned, aw.relevant, aw.runs, aw.relevant_pct) == (60, 12, 2, 20.0)
    assert (
        aw.active
        and aw.keyword_id == seeded.kw["ai workshop"]
        and aw.last_run_at is not None
    )
    ex = rows["excel training"]
    assert (ex.scanned, ex.relevant, ex.runs) == (40, 0, 2)
    assert (rows["python bootcamp"].scanned, rows["python bootcamp"].relevant) == (
        10,
        0,
    )
    assert not rows["old paused"].active and rows["old paused"].scanned == 80
    assert [r.text for r in keyword_yield(db)][:3] == [
        "ai workshop",
        "excel training",
        "python bootcamp",
    ]  # active first


def test_deleted_keyword_falls_back_to_text_and_since_filters(
    db: Session, seeded: Seed
) -> None:
    add_run_stat(db, None, "legacy term", 7, age_days=40)
    add_enquiry(db, "Legacy genuine post", hits=[(None, "legacy term")])
    db.commit()
    rows = by_text(keyword_yield(db))
    assert rows["legacy term"].keyword_id is None and not rows["legacy term"].active
    assert (rows["legacy term"].scanned, rows["legacy term"].relevant) == (7, 1)
    recent = by_text(keyword_yield(db, since=utcnow() - timedelta(days=10)))
    assert "legacy term" not in recent or recent["legacy term"].scanned == 0


def test_text_only_rows_map_to_a_live_keyword_case_insensitively(
    db: Session, seeded: Seed
) -> None:
    add_run_stat(db, None, "AI Workshop", 5)
    db.commit()
    assert by_text(keyword_yield(db))["ai workshop"].scanned == 65


def test_empty_database_is_empty(db: Session) -> None:
    assert keyword_yield(db) == []

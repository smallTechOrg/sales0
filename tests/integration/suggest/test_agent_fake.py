"""Suggestions agent with the deterministic fake LLM on a migrated SQLite DB."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.agent.suggest import run_suggestions
from sales0.config import reset_settings
from sales0.db.models import Keyword, LlmCall, Suggestion
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMBadResponse
from sales0.services import suggestions as svc
from sales0.services.costs_report import SpendCapReached
from tests.integration.suggest.conftest import Seed, add_enquiry
from tests.support.fake_llm import FakeLLMClient


def answer(seed: Seed) -> dict:
    return {
        "new_keywords": [
            {
                "phrase": "prompt engineering workshop",
                "rationale": "Seen in several posts",
                "evidence": [1, 12],
            },
            {
                "phrase": "AI Workshop",
                "rationale": "dup of existing",
                "evidence": [1],
            },  # existing keyword
            {
                "phrase": "quantum blockchain course",
                "rationale": "ungrounded",
                "evidence": [1],
            },
            {"phrase": "chatbot training", "rationale": "bad index", "evidence": [99]},
            {
                "phrase": "copilot adoption trainer",
                "rationale": "Operations teams",
                "evidence": [11],
            },
        ],
        "pause_keywords": [
            {
                "keyword_id": seed.kw["excel training"],
                "rationale": "Scanned 40, nothing genuine",
            },
            {
                "keyword_id": seed.kw["ai workshop"],
                "rationale": "model wrongly wants to pause a productive keyword",
            },
            {"keyword_id": seed.kw["python bootcamp"], "rationale": "too little data"},
            {"keyword_id": seed.kw["old paused"], "rationale": "already paused"},
        ],
    }


def rows(db: Session) -> list[Suggestion]:
    return list(db.scalars(select(Suggestion).order_by(Suggestion.id)))


def test_enforces_rules_and_records_llm_call(db: Session, seeded: Seed) -> None:
    fake = FakeLLMClient([answer(seeded)])
    batch = run_suggestions(db, fake)
    db.commit()
    got = {(r.kind, r.text) for r in rows(db)}
    assert got == {
        ("new_keyword", "prompt engineering workshop"),
        ("new_keyword", "copilot adoption trainer"),
        ("pause_keyword", "excel training"),
    }
    assert (
        batch.created == 3
        and batch.new_count == 2
        and batch.pause_count == 1
        and batch.llm_called
    )
    assert fake.calls == 1 and fake.requests[0].feature == "suggest"
    pause = next(r for r in rows(db) if r.kind == "pause_keyword")
    assert (
        pause.keyword_id == seeded.kw["excel training"]
        and pause.dedupe_key == "pause_keyword:excel training"
    )
    assert pause.rationale == "Scanned 40, nothing genuine"
    new = next(r for r in rows(db) if r.text == "prompt engineering workshop")
    assert (
        new.dedupe_key == "new_keyword:prompt engineering workshop"
        and new.prompt_version == "suggest-v1"
    )
    assert "Zentora Foods" in new.evidence_json
    assert any("already a keyword" in d for d in batch.dropped) and any(
        "out of range" in d for d in batch.dropped
    )
    call = db.scalars(select(LlmCall)).one()
    assert (
        call.feature == "suggest"
        and call.status == "ok"
        and call.prompt_version == "suggest-v1"
        and call.cost_usd_micros > 0
    )
    assert batch.cost_usd_micros == call.cost_usd_micros


def test_pause_created_even_if_model_omits_it_with_fallback_rationale(
    db: Session, seeded: Seed
) -> None:
    run_suggestions(db, FakeLLMClient([{"new_keywords": [], "pause_keywords": []}]))
    (r,) = rows(db)
    assert r.kind == "pause_keyword" and "40 posts" in r.rationale


def test_idempotent_second_run_adds_nothing(db: Session, seeded: Seed) -> None:
    run_suggestions(db, FakeLLMClient([answer(seeded)]))
    db.commit()
    first = [(r.id, r.dedupe_key) for r in rows(db)]
    again = run_suggestions(db, FakeLLMClient([answer(seeded)]))
    db.commit()
    assert again.created == 0 and [(r.id, r.dedupe_key) for r in rows(db)] == first


def test_max_new_caps_new_keywords(db: Session, seeded: Seed) -> None:
    batch = run_suggestions(db, FakeLLMClient([answer(seeded)]), max_new=1)
    assert batch.new_count == 1


def test_thin_data_skips_llm_without_cost(db: Session) -> None:
    add_enquiry(db, "Only one genuine post about training")
    add_enquiry(db, "A discard", verdict="discard")
    fake = FakeLLMClient([])
    batch = run_suggestions(db, fake)
    assert (
        fake.calls == 0
        and not batch.llm_called
        and batch.created == 0
        and "Not enough data" in batch.message
    )
    assert db.scalar(select(func.count()).select_from(LlmCall)) == 0


def test_spend_cap_exceeded_blocks_before_any_call(
    db: Session, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "1")
    reset_settings()
    db.add(LlmCall(feature="triage", model="m", status="ok", cost_usd_micros=2_000_000))
    db.commit()
    fake = FakeLLMClient([answer(seeded)])
    with pytest.raises(SpendCapReached) as ei:
        run_suggestions(db, fake)
    assert isinstance(ei.value, DomainError) and fake.calls == 0 and rows(db) == []


def test_repair_once_on_invalid_output_records_both_calls(
    db: Session, seeded: Seed
) -> None:
    fake = FakeLLMClient(
        [
            {
                "new_keywords": [{"phrase": "x", "rationale": "r", "evidence": [1]}],
                "pause_keywords": [],
            },
            answer(seeded),
        ]
    )
    batch = run_suggestions(db, fake)
    assert fake.calls == 2 and batch.created == 3
    assert (
        "Problems with your previous answer" in fake.requests[1].messages[0]["content"]
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(LlmCall)
            .where(LlmCall.feature == "suggest")
        )
        == 2
    )


def test_unusable_twice_raises_domain_error(db: Session, seeded: Seed) -> None:
    with pytest.raises(DomainError) as ei:
        run_suggestions(db, FakeLLMClient([None, LLMBadResponse("no tool")]))
    assert ei.value.code == "suggest_failed" and rows(db) == []


def test_auth_error_propagates_after_error_row(db: Session, seeded: Seed) -> None:
    with pytest.raises(LLMAuthError):
        run_suggestions(db, FakeLLMClient([LLMAuthError("bad key")]))
    call = db.scalars(select(LlmCall)).one()
    assert call.feature == "suggest" and call.status == "error"


def test_unique_race_does_not_crash(
    db: Session, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sales0.agent.suggest.loop as loop

    real = loop._insert
    monkeypatch.setattr(
        loop,
        "_insert",
        lambda d, row: (
            d.add(Suggestion(kind=row.kind, text=row.text, dedupe_key=row.dedupe_key)),
            d.flush(),
            real(d, row),
        )[2],
    )
    batch = run_suggestions(db, FakeLLMClient([answer(seeded)]))
    assert batch.created == 0  # every row lost the race; nothing crashed


def test_dismissed_and_accepted_are_never_resurrected(
    db: Session, seeded: Seed
) -> None:
    run_suggestions(db, FakeLLMClient([answer(seeded)]))
    db.commit()
    by = {r.text: r for r in rows(db)}
    svc.dismiss(db, by["prompt engineering workshop"].id)
    d = svc.accept(db, by["copilot adoption trainer"].id)
    assert (
        d.note is None
        and db.scalar(
            select(Keyword).where(Keyword.text_key == "copilot adoption trainer")
        )
        is not None
    )
    svc.accept(db, by["excel training"].id)
    db.commit()
    assert (
        db.scalar(select(Keyword.active).where(Keyword.text == "excel training"))
        is False
    )
    n = len(rows(db))
    again = run_suggestions(db, FakeLLMClient([answer(seeded)]))
    assert again.created == 0 and len(rows(db)) == n
    assert svc.pending_count(db) == 0


def test_service_edge_cases(db: Session, seeded: Seed) -> None:
    run_suggestions(db, FakeLLMClient([answer(seeded)]))
    db.commit()
    by = {r.text: r for r in rows(db)}
    # phrase added by hand in the meantime -> accepted with a note, not an error
    from tests.integration.suggest.conftest import add_keyword

    add_keyword(db, "Prompt Engineering Workshop")
    d = svc.accept(db, by["prompt engineering workshop"].id)
    assert d.suggestion.status == "accepted" and "already" in (d.note or "")
    with pytest.raises(DomainError) as e1:
        svc.accept(db, by["prompt engineering workshop"].id)
    assert e1.value.code == "suggestion_decided"
    with pytest.raises(DomainError) as e2:
        svc.dismiss(db, 9999)
    assert e2.value.code == "not_found"
    # stale pause: keyword deleted
    from sales0.services.keywords import remove_keyword

    remove_keyword(db, seeded.kw["excel training"])
    with pytest.raises(DomainError) as e3:
        svc.accept(db, by["excel training"].id)
    assert (
        e3.value.code == "suggestion_stale" and by["excel training"].status == "pending"
    )
    assert [s.status for s in svc.list_suggestions(db, "accepted")] == ["accepted"]

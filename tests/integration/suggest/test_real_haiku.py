"""Real Haiku (key from .env): the whole agent on synthetic data with one clearly dead keyword."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.agent.suggest import SuggestionBatch, run_suggestions
from sales0.agent.suggest.rules import norm_phrase, terms
from sales0.db.models import Keyword, LlmCall, Suggestion
from sales0.llm import get_llm_client
from tests.integration.suggest.conftest import Seed

pytestmark = pytest.mark.llm


def test_real_run_is_schema_valid_grounded_and_only_pauses_the_dead_keyword(
    db: Session, seeded: Seed
) -> None:
    batch = run_suggestions(db, get_llm_client())
    db.commit()
    assert isinstance(batch, SuggestionBatch) and batch.llm_called
    rows = list(db.scalars(select(Suggestion)))
    assert rows, batch.message
    existing = {norm_phrase(k.text) for k in db.scalars(select(Keyword))}
    new = [r for r in rows if r.kind == "new_keyword"]
    assert len(new) <= 5
    for r in new:
        assert 2 <= len(r.text) <= 100 and norm_phrase(r.text) not in existing
        assert (
            r.dedupe_key == f"new_keyword:{norm_phrase(r.text)}"
            and r.status == "pending"
        )
        snippets = [e["snippet"] for e in json.loads(r.evidence_json)]
        assert snippets, r.text
        assert any(terms(r.text) & terms(s) for s in snippets), (
            f"{r.text!r} not grounded"
        )
        assert len(r.rationale) <= 300
    pauses = [r for r in rows if r.kind == "pause_keyword"]
    assert [p.keyword_id for p in pauses] == [
        seeded.kw["excel training"]
    ]  # the only active keyword with 30+ scans and 0 genuine
    assert pauses[0].rationale
    call = db.scalars(select(LlmCall).where(LlmCall.feature == "suggest")).first()
    assert (
        call is not None
        and call.status == "ok"
        and call.cost_usd_micros > 0
        and call.input_tokens > 0
    )
    assert call.prompt_version == "suggest-v1" and 1 <= batch.llm_calls <= 2
    # a second real-shaped run never duplicates what is stored
    n = len(rows)
    run_suggestions(db, get_llm_client())
    db.commit()
    keys = [r.dedupe_key for r in db.scalars(select(Suggestion))]
    assert len(keys) == len(set(keys)) >= n

"""Triage persistence: ai_* + editable fields, llm_calls rows, history, failures and fatal errors.

The FakeLLMClient tests pin the persistence contract deterministically; the ``llm``-marked
tests make real Haiku calls with the key from .env (no fakes on that path).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.config import get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, LlmCall, StatusHistory
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMTransientError, reset_llm_client
from sales0.llm.pricing import cost_usd_micros
from sales0.services import inbox
from sales0.services.triage import triage_enquiry
from tests.integration.services.helpers import make_enquiry
from tests.support.fake_llm import FakeLLMClient, triage_answer

P1_TEXT = (
    "We are looking for a training partner! Acme Logistics wants a training partner to run a 2-day Generative AI "
    "workshop for 40 managers at our Pune office. The sessions will be offline (in person), in the week of 17 Nov. "
    "Please send your proposals to training@acmelogistics.example or call +91 98200 11111."
)


def _llm_rows(eid: int) -> list[LlmCall]:
    with session_scope() as db:
        return list(db.scalars(select(LlmCall).where(LlmCall.enquiry_id == eid).order_by(LlmCall.id)).all())


def test_triage_enquiry_persists_ai_fields_editable_copies_cost_and_history(db_url: str) -> None:
    eid = make_enquiry(P1_TEXT)
    fake = FakeLLMClient([triage_answer(email="ceo@invented.example", offering_id=999)])
    with session_scope() as db:
        outcome = triage_enquiry(db, eid, fake, run_id=None)
    assert outcome.status == "done"
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None
        assert (e.triage_status, e.ai_verdict, e.priority, e.ai_priority) == ("done", "genuine", "high", "high")
        assert e.email is None  # guard: absent from the text
        assert e.offering_id is None and e.ai_offering_id is None  # guard: not in the catalogue
        assert e.company == "Acme Logistics" and e.ai_extraction is not None
        assert e.prompt_version == "triage-v1" and e.triaged_at is not None
        hist = db.scalars(select(StatusHistory).where(StatusHistory.entity_id == eid,
                                                      StatusHistory.field == "triage_status")).one()
        assert (hist.from_value, hist.to_value) == ("pending", "done")
    [row] = _llm_rows(eid)
    assert (row.status, row.feature, row.fewshot_count) == ("ok", "triage", 0)
    assert row.cost_usd_micros == cost_usd_micros(1200, 150, get_settings())


def test_repair_answer_writes_two_llm_rows_and_card_cost_sums_them(db_url: str) -> None:
    eid = make_enquiry(P1_TEXT)
    fake = FakeLLMClient([{"verdict": "maybe"}, triage_answer()])
    with session_scope() as db:
        assert triage_enquiry(db, eid, fake).status == "done"
    rows = _llm_rows(eid)
    assert len(rows) == 2
    with session_scope() as db:
        assert inbox.get_card(db, eid).cost_usd_micros == sum(r.cost_usd_micros for r in rows)


def test_transient_failure_marks_enquiry_failed_with_an_error_row(db_url: str) -> None:
    eid = make_enquiry(P1_TEXT)
    fake = FakeLLMClient([LLMTransientError("LLM unavailable — retry later (APITimeoutError)")])
    with session_scope() as db:
        outcome = triage_enquiry(db, eid, fake)
    assert outcome.status == "failed"
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None and e.triage_status == "failed" and e.triage_error
    [row] = _llm_rows(eid)
    assert row.status == "error" and row.cost_usd_micros == 0


def test_fatal_auth_error_propagates_and_leaves_enquiry_pending(db_url: str) -> None:
    eid = make_enquiry(P1_TEXT)
    fake = FakeLLMClient([LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env")])
    with session_scope() as db, pytest.raises(LLMAuthError):
        triage_enquiry(db, eid, fake)
    with session_scope() as db:
        assert db.get(Enquiry, eid).triage_status == "pending"  # type: ignore[union-attr]


def test_triage_missing_enquiry_is_not_found(db_url: str) -> None:
    with session_scope() as db, pytest.raises(DomainError) as info:
        triage_enquiry(db, 404, FakeLLMClient([triage_answer()]))
    assert info.value.code == "not_found"


@pytest.mark.llm
def test_retry_triage_with_an_invalid_key_returns_llm_auth_and_keeps_the_card(db_url: str,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_ANTHROPIC_API_KEY", "sk-ant-invalid-slice-d-test")
    reset_settings()
    reset_llm_client()
    eid = make_enquiry(P1_TEXT)
    with session_scope() as db, pytest.raises(DomainError) as info:
        inbox.retry_triage(db, eid)
    assert info.value.code == "llm_auth" and info.value.http_status == 502
    assert "Anthropic rejected the key" in info.value.message
    with session_scope() as db:
        assert db.get(Enquiry, eid).triage_status == "pending"  # type: ignore[union-attr]
    [row] = _llm_rows(eid)
    assert (row.status, row.error_type) == ("error", "auth")


@pytest.mark.llm
def test_real_haiku_triage_of_p1_extracts_the_buyer_and_records_real_cost(db_url: str) -> None:
    eid = make_enquiry(P1_TEXT, n=1)
    with session_scope() as db:
        card = inbox.retry_triage(db, eid)
    assert card.triage_status == "done" and card.ai_verdict == "genuine"
    assert card.email == "training@acmelogistics.example"
    assert card.city and "pune" in card.city.lower()
    assert card.mode == "offline" and card.priority == "high"
    assert card.ai_reason and 1 <= len(card.ai_reason) <= 160 and "\n" not in card.ai_reason
    rows = _llm_rows(eid)
    assert rows and all(r.input_tokens > 0 and r.output_tokens > 0 for r in rows)
    assert all(r.cost_usd_micros == cost_usd_micros(r.input_tokens, r.output_tokens, get_settings()) for r in rows)

"""Retry all failed AI triage in one click: To review (enquiries) and Skipped by AI (posts), with early stops."""

from __future__ import annotations

from html import unescape

import pytest
from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, SeenPost
from sales0.llm import LLMAuthError, LLMTransientError, set_llm_client
from sales0.services import triage_retry
from tests.integration.web.conftest import HX, make_enquiry, make_run, make_seen_post
from tests.support.fake_llm import FakeLLMClient, triage_answer


def _failed_enquiries(n: int) -> list[int]:
    run_id = make_run("succeeded")
    return [make_enquiry("P1", text=f"We need a corporate GenAI workshop for our managers, batch {i}", run_id=run_id,
                         triage_status="failed", triage_error="Anthropic timed out") for i in range(n)]  # fmt: skip


def _status(eid: int) -> str:
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None
        return e.triage_status


def test_button_shows_the_count_only_when_something_failed(client: TestClient) -> None:
    assert 'id="retry-all-failed"' not in client.get("/inbox").text
    _failed_enquiries(2)
    page = client.get("/inbox").text
    assert 'id="retry-all-failed"' in page and "Retry all failed (2)" in page


def test_retry_all_triages_every_failed_enquiry(client: TestClient) -> None:
    ids = _failed_enquiries(3)
    fake = FakeLLMClient(responder=lambda req: triage_answer())
    set_llm_client(fake)
    resp = client.post("/inbox/retry-failed", headers=HX)
    body = unescape(resp.text)
    assert resp.status_code == 200 and "AI triage retried: 3 done." in body
    assert all(_status(i) == "done" for i in ids)
    assert 'id="retry-all-failed"' not in body  # nothing left


def test_retry_all_stops_when_the_ai_key_is_rejected(client: TestClient) -> None:
    ids = _failed_enquiries(3)
    set_llm_client(FakeLLMClient(responder=lambda req: LLMAuthError("Anthropic rejected the key")))
    body = unescape(client.post("/inbox/retry-failed", headers=HX).text)
    assert "Stopped: Anthropic rejected the key" in body and "3 still to retry" in body
    assert all(_status(i) == "failed" for i in ids)


def test_retry_all_stops_after_three_failures_in_a_row(client: TestClient) -> None:
    ids = _failed_enquiries(5)
    fake = FakeLLMClient(responder=lambda req: LLMTransientError("LLM unavailable - retry later"))
    set_llm_client(fake)
    body = unescape(client.post("/inbox/retry-failed", headers=HX).text)
    assert "3 failed again" in body and "the AI looks unavailable" in body and "5 still to retry" in body
    assert sum(_status(i) == "failed" for i in ids) == 5


def test_a_success_resets_the_failure_streak(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _failed_enquiries(5)
    script = iter([LLMTransientError("x"), LLMTransientError("x"), triage_answer(), LLMTransientError("x"), triage_answer()])
    set_llm_client(FakeLLMClient(responder=lambda req: next(script)))
    monkeypatch.setattr(triage_retry, "STOP_AFTER_FAILURES", 3)
    body = unescape(client.post("/inbox/retry-failed", headers=HX).text)
    assert "AI triage retried: 2 done, 3 failed again." in body and "3 still to retry" in body
    assert sum(_status(i) == "done" for i in ids) == 2


def test_retry_all_is_refused_while_a_run_is_triaging(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _failed_enquiries(1)
    from sales0.jobs import runner

    monkeypatch.setattr(runner.get_runner(), "busy", lambda: "run")
    body = unescape(client.post("/inbox/retry-failed", headers=HX).text)
    assert "LinkedIn is busy" in body


def test_skipped_tab_retry_all_triages_failed_posts_with_text(client: TestClient) -> None:
    text = ("Hiring freelance AI trainers for upcoming batches, rates per day, apply now. "
            "We are a training agency sourcing trainers for our corporate clients across India.")
    sids = [make_seen_post(text + f" ({i})", verdict=None, reason=None, triage_status="failed", triage_error="Rate limited")
            for i in range(2)]  # fmt: skip
    page = client.get("/inbox?tab=skipped&state=failed").text
    assert "Retry all failed (2)" in page
    set_llm_client(FakeLLMClient(responder=lambda req: triage_answer(verdict="discard", reason="Recruiting trainers")))
    body = unescape(client.post("/inbox/skipped/retry-failed", headers=HX).text)
    assert "AI triage retried: 2 done." in body
    with session_scope() as db:
        assert all(db.get(SeenPost, s).triage_status == "done" for s in sids)

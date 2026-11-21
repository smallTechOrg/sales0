"""The AI being down is detected and shown (banner, Health page, follow-up form), and the follow-up form always works:
How / When / Who / Notes / reminder are saved as filled in with no AI call, and any AI failure still keeps the note."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import local_today
from sales0.config import get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Contact, FollowUpNote, LlmCall, Prospect, Reminder
from sales0.llm import LLMAuthError, LLMBadResponse, health
from sales0.llm.client import StructuredRequest
from sales0.services import ai_status
from sales0.services.crm import prospects
from sales0.web.app import create_app
from sales0.web.routes import pipeline as pipeline_routes
from tests.integration.web.conftest import HX
from tests.support.fake_llm import FakeLLMClient

TODAY = lambda: local_today(get_settings().tz)  # noqa: E731
CREDITS = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."


class Broken(FakeLLMClient):
    def __init__(self, exc: Exception) -> None:
        super().__init__(responder=lambda req: {})
        self.exc = exc

    def structured(self, req: StructuredRequest) -> Any:
        self.requests.append(req)
        raise self.exc


@pytest.fixture
def keyed(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SALES0_ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("SALES0_FOLLOWUP_AUTO_SCHEDULE", "false")
    reset_settings()
    health.reset()
    yield
    health.reset()
    reset_settings()


@pytest.fixture
def nokey(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SALES0_ANTHROPIC_API_KEY", "")
    reset_settings()
    yield
    reset_settings()


def _app(llm: FakeLLMClient) -> TestClient:
    app = create_app()
    app.dependency_overrides[pipeline_routes.get_llm] = lambda: llm
    app.dependency_overrides[pipeline_routes.get_llm_optional] = lambda: llm
    return TestClient(app)


def _prospect() -> tuple[int, int, int]:
    with session_scope() as db:
        v = prospects.create_manual(db, name="Priya Menon", company="Skillmart", email="priya@skillmart.example")
        p = db.get(Prospect, v.id)
        cid = db.scalar(select(Contact.id).where(Contact.full_name == "Priya Menon"))
        return v.id, int(p.company_id), int(cid)


def test_status_up_by_default_and_down_on_a_credits_error(keyed: None) -> None:
    with session_scope() as db:
        assert ai_status.status(db).down is False
        health.note_failure("bad_response", CREDITS, "summary")
        st = ai_status.status(db)
        assert st.down and "out of credits" in st.reason and "console.anthropic.com" in st.fix
        health.note_success()
        assert ai_status.status(db).down is False


def test_status_from_stored_calls_after_a_restart(keyed: None) -> None:
    with session_scope() as db:
        db.add(LlmCall(feature="summary", model="m", prompt_version="v", status="error", error_type="bad_response"))
        db.flush()
        assert ai_status.status(db).down is False  # one odd answer is not an outage
        db.add(LlmCall(feature="summary", model="m", prompt_version="v", status="error", error_type="auth"))
        db.flush()
        st = ai_status.status(db)
        assert st.down and "rejected the API key" in st.reason


def test_no_key_is_down(nokey: None) -> None:
    with session_scope() as db:
        st = ai_status.status(db)
        assert st.down and st.reason == "no API key is set"


def test_banner_and_health_page_say_the_ai_is_down(keyed: None) -> None:
    health.note_failure("bad_response", CREDITS, "summary")
    with _app(FakeLLMClient(responder=lambda r: {})) as c:
        page = unescape(c.get("/pipeline").text)
        assert 'id="ai-down-banner"' in page and "out of credits" in page
        hp = unescape(c.get("/health").text)
        assert "out of credits" in hp and 'id="health-check-ai"' in hp
    health.reset()
    with _app(FakeLLMClient(responder=lambda r: {})) as c:
        assert 'id="ai-down-banner"' not in c.get("/pipeline").text


def test_form_with_how_saves_as_filled_in_without_the_ai(keyed: None) -> None:
    llm = Broken(LLMAuthError("rejected"))
    pid, company_id, contact_id = _prospect()
    when = TODAY() - timedelta(days=2)
    remind = TODAY() + timedelta(days=4)
    with _app(llm) as c:
        page = unescape(c.get(f"/companies/{company_id}").text)
        for el in ('id="fu-how"', 'id="fu-when"', 'id="fu-who"', 'id="fu-remind-on"', 'id="fu-remind-note"', ">WhatsApp<", ">Priya Menon<"):
            assert el in page
        r = c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=HX, data={
            "how": "whatsapp", "happened_on": when.isoformat(), "contact_id": str(contact_id), "text": "Sent the deck on WhatsApp",
            "remind_on": remind.isoformat(), "remind_note": "Ask about the deck"})
        assert r.status_code == 200 and "Follow-up logged (WhatsApp," in unescape(r.text)
    assert llm.requests == []  # no AI call at all
    with session_scope() as db:
        note = db.scalar(select(FollowUpNote))
        assert (note.how, note.happened_on, note.contact_id, note.contact_name, note.structured_by) == (
            "whatsapp", when, contact_id, "Priya Menon", "person")
        rem = db.get(Reminder, note.reminder_id)
        assert rem.due_on == remind and rem.note == "Ask about the deck"


@pytest.mark.parametrize("exc", [LLMAuthError("rejected"), LLMBadResponse(CREDITS)])
def test_note_is_kept_when_the_ai_fails(keyed: None, exc: Exception) -> None:
    pid, _company_id, _cid = _prospect()
    with _app(Broken(exc)) as c:
        r = c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=HX, data={"text": "Called them, call back Friday"})
        assert r.status_code == 200 and "saved as typed" in unescape(r.text)
    with session_scope() as db:
        note = db.scalar(select(FollowUpNote))
        assert note.structured_by == "fallback" and note.raw_text == "Called them, call back Friday"
        assert db.scalar(select(LlmCall.error_type).where(LlmCall.status == "error")) == exc.error_type


def test_form_rejects_bad_values(keyed: None) -> None:
    pid, _company_id, _cid = _prospect()
    with _app(Broken(LLMAuthError("x"))) as c:
        js = {**HX, "Accept": "application/json"}
        future = (TODAY() + timedelta(days=1)).isoformat()
        assert c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=js, data={"how": "fax", "text": "hello there"}).status_code == 400
        assert c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=js,
                      data={"how": "call", "happened_on": future, "text": "hello there"}).status_code == 400
        assert c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=js,
                      data={"how": "call", "contact_id": "999999", "text": "hello there"}).status_code == 400


def test_check_ai_now_button(nokey: None) -> None:
    with TestClient(create_app()) as c:
        r = c.post("/health/ai-check", follow_redirects=True)
        assert r.status_code == 200 and "no API key is set" in unescape(r.text)
    with session_scope() as db:
        assert db.scalar(select(LlmCall.feature).where(LlmCall.feature == "ping")) == "ping"


def test_form_saves_with_no_api_key_at_all(nokey: None) -> None:
    pid, _company_id, _cid = _prospect()
    with TestClient(create_app()) as c:  # the real dependency: no key -> no client
        r = c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=HX, data={"how": "email", "text": "Emailed the brochure"})
        assert r.status_code == 200 and "Follow-up logged (Email," in unescape(r.text)
        r = c.post(f"/pipeline/prospects/{pid}/followup-notes", headers=HX, data={"text": "Typed with no How"})
        assert r.status_code == 200 and "saved as typed" in unescape(r.text)

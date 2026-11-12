"""q23 round: enquiry categories -> company characteristics (Partner / Direct client) set by the AI, Refresh all,
company offerings (several per company, filters, auto-tag), offering groups, research key people, drafting
instructions, inbox company links, and the free-text follow-up log with reusable reminders."""

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
from sales0.db.models import Company, Contact, FollowUp, FollowUpNote, Offering, OfferingGroup, Prospect, Reminder
from sales0.llm.client import StructuredRequest
from sales0.services import reminders, text_settings
from sales0.services.crm import characteristics, company_offerings, followup_log, prospects
from sales0.services.crm.actor import Actor
from sales0.services.research import summary as summary_service
from sales0.web.app import create_app
from sales0.web.routes import contacts as contacts_routes
from sales0.web.routes import pipeline as pipeline_routes
from tests.integration.web.conftest import HX, make_enquiry, triaged
from tests.support.fake_llm import FakeLLMClient

TODAY = lambda: local_today(get_settings().tz)  # noqa: E731

SUMMARY = {
    "who_they_are": "Skillmart, a corporate training company, Pune.", "what_they_want": "Trainers for client workshops.",
    "where_we_stand": "New prospect.", "characteristics": [], "open_follow_ups": [], "next_best_step": "Call them.",
    "relationship": "partner", "fit_offerings": ["AI corporate workshops", "Not an offering"],
}  # fmt: skip


def answer(req: StructuredRequest) -> Any:
    if req.tool_name == "record_account_summary":
        return dict(SUMMARY)
    if req.tool_name == "record_follow_up":
        note = req.messages[0]["content"]
        if "nothing structured" in note:
            return {"how": "nonsense"}
        return {"how": "call", "date": (TODAY() - timedelta(days=1)).isoformat(), "contact": "priya",
                "notes": "Priya liked the proposal; needs CFO approval.", "remind_on": (TODAY() + timedelta(days=5)).isoformat(),
                "remind_note": "Call Priya about CFO approval"}
    raise AssertionError(req.tool_name)


@pytest.fixture
def llm() -> FakeLLMClient:
    return FakeLLMClient(responder=answer)


@pytest.fixture
def web(db_url: str, llm: FakeLLMClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SALES0_FOLLOWUP_AUTO_SCHEDULE", "false")
    reset_settings()
    app = create_app()
    app.dependency_overrides[pipeline_routes.get_llm] = lambda: llm
    app.dependency_overrides[contacts_routes.get_llm] = lambda: llm
    with TestClient(app) as c:
        yield c
    reset_settings()


def _prospect(name: str = "Priya Menon", company: str = "Skillmart") -> tuple[int, int]:
    with session_scope() as db:
        v = prospects.create_manual(db, name=name, company=company, email=f"{name.split()[0].lower()}@{company.lower()}.example")
        return v.id, int(db.get(Prospect, v.id).company_id)


def _offering_id(name: str) -> int:
    with session_scope() as db:
        return int(db.scalar(select(Offering.id).where(Offering.name == name)))


# ---------------------------------------------------------------------- categories -> characteristics + refresh all
def test_summary_tags_partner_and_offerings_only_when_missing(web: TestClient, llm: FakeLLMClient) -> None:
    pid, cid = _prospect()
    with session_scope() as db:
        summary_service.refresh_summary(db, llm, None, pid, actor=Actor("Ann"))
        names = [c.name for c in characteristics.for_prospects(db, [pid])[pid]]
        assert names == ["Partner"]
        assert company_offerings.names_by_company(db, [cid])[cid] == ["AI corporate workshops"]  # unknown name ignored
        req = next(r for r in llm.requests if r.tool_name == "record_account_summary")
        assert "AI corporate workshops" in req.messages[0]["content"]  # the offerings list is in the prompt
    # a person's choice is kept: switch to Direct client + another offering, refresh again -> unchanged
    SUMMARY_DC = {**SUMMARY, "relationship": "direct_client", "fit_offerings": ["Products"]}
    llm._responder = lambda req: SUMMARY_DC if req.tool_name == "record_account_summary" else answer(req)  # noqa: SLF001
    with session_scope() as db:
        summary_service.refresh_summary(db, llm, None, pid, actor=Actor("Ann"))
        assert [c.name for c in characteristics.for_prospects(db, [pid])[pid]] == ["Partner"]
        assert company_offerings.names_by_company(db, [cid])[cid] == ["AI corporate workshops"]


def test_refresh_all_updates_summary_and_tags_without_research(web: TestClient, llm: FakeLLMClient) -> None:
    a, ca = _prospect("Priya Menon", "Skillmart")
    b, cb = _prospect("Ravi Rao", "Beta Foods")
    page = unescape(web.get("/companies").text)
    assert 'id="btn-refresh-all"' in page and "Refresh all 2" in page and 'id="btn-refresh-selected"' in page
    r = web.post("/companies/research", headers=HX, data={"mode": "refresh", "all_matching": "1"})
    assert "Refreshed 2 companies (summary, Partner / Direct client and offerings)" in unescape(r.text)
    assert all(req.tool_name == "record_account_summary" for req in llm.requests)  # no web research, no Sonnet
    with session_scope() as db:
        assert set(company_offerings.names_by_company(db, [ca, cb])) == {ca, cb}


# ---------------------------------------------------------------------- company offerings + filters + groups
def test_company_offerings_editor_and_filters(web: TestClient) -> None:
    pid, cid = _prospect()
    _p2, other = _prospect("Ravi Rao", "Beta Foods")
    ws, prod = _offering_id("AI corporate workshops"), _offering_id("Products")
    r = web.post(f"/companies/{cid}/offerings", data={"offering_ids": [ws, prod]})
    assert r.status_code == 200 and "Offerings saved (2)." in unescape(r.text)
    page = unescape(web.get(f"/companies/{cid}").text)
    assert 'id="company-offerings"' in page and f'value="{ws}" checked' in page and f'value="{prod}" checked' in page
    only = unescape(web.get(f"/companies?offering={prod}").text)
    assert "Skillmart" in only and "Beta Foods" not in only and 'id="company-offering"' in only
    board = unescape(web.get(f"/pipeline?tab=prospects&offering={prod}").text)
    assert 'id="f-offering"' in board and f'id="company-{cid}"' in board and f'id="company-{other}"' not in board
    assert '>Products</span>' in board and 'chip--offering' in board


def test_setup_offerings_section_groups_rename_merge_and_reorder(web: TestClient) -> None:
    html = unescape(web.get("/setup/offerings").text)
    assert 'aria-current="page">Offerings' in html and "Rename group" in html
    with session_scope() as db:
        groups = {g.name: g.id for g in db.scalars(select(OfferingGroup))}
    r = web.post(f"/setup/offering-groups/{groups['Services']}", headers=HX, data={"name": "Consulting"})
    assert "Group renamed to 'Consulting'." in unescape(r.text)
    web.post(f"/setup/offering-groups/{groups['Products']}/move", headers=HX, data={"direction": "up"})
    r = web.post(f"/setup/offering-groups/{groups['Products']}", headers=HX, data={"name": "training"})  # merge into Training
    assert "Group renamed to 'Training'." in unescape(r.text)
    with session_scope() as db:
        assert db.get(OfferingGroup, groups["Products"]) is None
        products = db.scalar(select(Offering).where(Offering.name == "Products"))
        assert products.group_id == groups["Training"]
        names = [g.name for g in db.scalars(select(OfferingGroup).order_by(OfferingGroup.sort_order))]
        assert names == ["Training", "Consulting"]


# ---------------------------------------------------------------------- research key people
def test_key_people_become_contacts_and_invented_details_are_dropped(db_url: str) -> None:
    from sales0.agent.research.schemas import CompanyBrief
    from sales0.agent.research.synth import _clean
    from sales0.services.research.prospect import save_key_people

    page = "Leadership: Anita Rao, CHRO. Reach her at anita.rao@skillmart.example. https://www.linkedin.com/in/anita-rao-1/"
    brief = CompanyBrief.model_validate({
        "company_name": "Skillmart", "what_they_do": "Training", "key_people": [
            {"name": "Anita Rao", "title": "CHRO", "department": "HR", "email": "anita.rao@skillmart.example",
             "linkedin_url": "https://www.linkedin.com/in/anita-rao-1/", "phone": "+91 98765 43210", "source": "https://skillmart.example/team"},
            {"name": "Ghost Person", "title": "CEO", "email": "ceo@skillmart.example", "source": "https://made-up.example/"},
        ]})
    clean = _clean(brief, {"https://skillmart.example/team"}, [], page)
    assert [(p.name, p.email, p.phone, p.linkedin_url) for p in clean.key_people] == [
        ("Anita Rao", "anita.rao@skillmart.example", "", "https://www.linkedin.com/in/anita-rao-1/")]  # phone not in the input
    pid, cid = _prospect()
    with session_scope() as db:
        n = save_key_people(db, pid, cid, [p.model_dump() for p in clean.key_people])
        row = db.scalar(select(Contact).where(Contact.full_name == "Anita Rao"))
        assert n == 1 and row.company_id == cid and row.title == "CHRO - HR" and row.linkedin_url
        assert save_key_people(db, pid, cid, [p.model_dump() for p in clean.key_people]) == 0  # found again, not duplicated


# ---------------------------------------------------------------------- drafting instructions + inbox link
def test_drafting_instructions_reach_the_draft_system_prompt(web: TestClient) -> None:
    from sales0.services.outreach.drafting import system_prompt

    r = web.post("/outreach/instructions", data={"text": "Always sign off as Team smallTech."})
    assert r.status_code == 200 and "Drafting instructions saved" in unescape(r.text)
    assert "Always sign off as Team smallTech." in web.get("/outreach/instructions").text
    with session_scope() as db:
        assert text_settings.get(db, text_settings.DRAFTING_INSTRUCTIONS) == "Always sign off as Team smallTech."
        for follow_up in (False, True):
            assert system_prompt(db, follow_up).rstrip().endswith("Always sign off as Team smallTech.")
        text_settings.put(db, text_settings.DRAFTING_INSTRUCTIONS, "")
        assert "Team instructions" not in system_prompt(db, False)


def test_inbox_company_name_links_to_the_company_page(web: TestClient) -> None:
    eid = make_enquiry("P1", **{**triaged("genuine", "Buyer"), "company": "Linky Co"})
    with session_scope() as db:
        co = Company(name="Linky Co", name_key="linky co")
        db.add(co)
        db.flush()
        from sales0.db.models import Enquiry

        db.get(Enquiry, eid).company_id = co.id
        cid = co.id
    assert f'<a href="/companies/{cid}" class="enquiry__company-link">Linky Co</a>' in web.get("/inbox").text


# ---------------------------------------------------------------------- follow-up log + reminders
def test_free_text_follow_up_is_structured_logged_and_sets_a_reminder(web: TestClient) -> None:
    pid, cid = _prospect()
    r = web.post(f"/pipeline/prospects/{pid}/followup-notes", headers=HX,
                 data={"text": "Spoke to priya yesterday on the phone, she liked it, CFO approval needed. call again in 5 days"})
    text = unescape(r.text)
    assert "Follow-up logged (Call," in text and "reminder set for" in text
    with session_scope() as db:
        note = db.scalar(select(FollowUpNote))
        assert (note.how, note.happened_on, note.contact_name) == ("call", TODAY() - timedelta(days=1), "Priya Menon")
        assert note.contact_id is not None and note.structured_by == "ai"
        rem = db.get(Reminder, note.reminder_id)
        assert rem.due_on == TODAY() + timedelta(days=5) and rem.prospect_id == pid and rem.company_id == cid
        p = db.get(Prospect, pid)
        assert p.stage == "new" and p.contacted_at is not None  # q25: recorded, the stage is not moved
        assert db.scalars(select(FollowUp).where(FollowUp.prospect_id == pid)).all() == []  # nothing pre-scheduled
    page = unescape(web.get(f"/companies/{cid}").text)
    assert 'id="fu-note-form"' in page and "Priya liked the proposal; needs CFO approval." in page and 'id="fu-reminders"' in page
    assert 'id="log-contact-form"' not in page  # the old structured Log contact form is gone (auto-schedule off)


def test_unstructured_note_is_kept_as_typed_and_reminders_are_reusable(web: TestClient) -> None:
    pid, cid = _prospect()
    web.post(f"/pipeline/prospects/{pid}/followup-notes", headers=HX, data={"text": "nothing structured here, just a thought"})
    with session_scope() as db:
        note = db.scalar(select(FollowUpNote))
        assert note.how == "other" and note.happened_on == TODAY() and note.reminder_id is None
        # the module works for any subject: a company-level reminder due today
        due = reminders.add(db, subject_type="company", subject_id=cid, company_id=cid, due_on=TODAY(), note="Send the deck")
        overdue = reminders.add(db, subject_type="prospect", subject_id=pid, prospect_id=pid, company_id=cid,
                                due_on=TODAY() - timedelta(days=2), note="Chase")
        rid, oid = due.id, overdue.id
    board = unescape(web.get("/pipeline").text)
    assert 'id="due-group-reminders"' in board and "Send the deck" in board and "Chase" in board
    r = web.post(f"/reminders/{rid}/done", headers=HX, data={})
    assert "Reminder done." in unescape(r.text)
    r = web.post(f"/reminders/{oid}/snooze", headers=HX, data={"due_on": (TODAY() + timedelta(days=3)).isoformat()})
    assert "Reminder moved to" in unescape(r.text)
    with session_scope() as db:
        assert reminders.due(db) == [] and reminders.due_count(db) == 0
        assert len(reminders.open_for(db, prospect_id=pid)) == 1
    r = web.post(f"/pipeline/prospects/{pid}/reminders", headers=HX, data={"due_on": TODAY().isoformat(), "note": "Ping"})
    assert "Reminder set for" in unescape(r.text)
    with session_scope() as db:
        assert followup_log.notes_for(db, pid)[0].how_label == "Other"


def test_follow_up_note_validation(web: TestClient) -> None:
    pid, _cid = _prospect()
    assert web.post(f"/pipeline/prospects/{pid}/followup-notes", headers={**HX, "Accept": "application/json"}, data={"text": "a"}).status_code == 400

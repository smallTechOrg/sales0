"""Account summary (one call, versioned, human edits) and our notes (authored entries; add / edit / delete own)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import LlmCall, Prospect, ProspectNote, TeamMember
from sales0.errors import DomainError
from sales0.llm import LLMTransientError
from sales0.services.crm import notes as notes_service
from sales0.services.crm import prospects
from sales0.services.crm.actor import Actor
from sales0.services.outreach import drafting
from sales0.services.research import docs
from sales0.services.research import prospect as research
from sales0.services.research import summary as summary_service
from tests.integration.research.conftest import make_prospect, responder
from tests.support.fake_llm import FakeLLMClient


def _member(db, name: str, role: str = "member") -> TeamMember:
    m = TeamMember(name=name, email=f"{name.lower()}@smalltech.example", role=role, active=True)
    db.add(m)
    db.flush()
    return m


def test_summary_is_one_call_over_everything_and_versioned(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None, notes="Prefers WhatsApp; budget approved in Q4.")
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        research.research_prospect(db, llm, None, pid, actor=Actor("Ann"), queue_linkedin=False)
        before = len(llm.requests)
        s = summary_service.refresh_summary(db, llm, None, pid, actor=Actor("Ann"))
        assert len(llm.requests) == before + 1
        req = llm.requests[-1]
        assert req.feature == "summary" and req.tool_name == "record_account_summary"
        text = req.messages[0]["content"]
        assert "budget approved in Q4" in text and "Latest research (version 1" in text and "<records>" in text
        assert s.version == 2 and s.origin == "ai" and s.body.startswith("Who they are: Acme Logistics")  # v1 came with the research
        assert db.scalar(select(LlmCall.feature).order_by(LlmCall.id.desc()).limit(1)) == "summary"
        ravi = _member(db, "Ravi")
        edited = summary_service.edit_summary(db, pid, s.body + "\nAlso: decision maker is the COO.", actor=Actor("Ravi", ravi.id))
        assert edited.version == 3 and edited.origin == "human" and edited.based_on_version == 2
        assert summary_service.summary_for_drafting(db, pid).endswith("decision maker is the COO.")
        with pytest.raises(DomainError):
            summary_service.edit_summary(db, pid, "  ", actor=Actor("Ravi"))


def test_failed_summary_is_recorded_and_not_used_for_drafting(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None)
    with session_scope() as db:
        s = summary_service.refresh_summary(db, FakeLLMClient([LLMTransientError("down")]), None, pid, actor=Actor("Ann"))
        assert s.status == "failed" and s.body is None and "unavailable" in (s.error or "")
        assert summary_service.summary_for_drafting(db, pid) is None
        assert docs.latest(db, pid, "summary").version == 1


def test_drafting_gets_the_latest_summary_as_background(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None)
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        member = _member(db, "Boss", "admin")
        summary_service.edit_summary(db, pid, "Where we stand: they asked for a December slot.", actor=Actor("Boss", member.id))
        drafting.generate_draft(
            db, llm, member, to="priya@acme.example", company="Acme Logistics", requirement="GenAI workshop",
            account_summary=summary_service.summary_for_drafting(db, pid), prospect_id=pid,
        )  # fmt: skip
    req = next(r for r in llm.requests if r.tool_name == "write_email")
    assert "<account_summary>" in req.messages[0]["content"] and "December slot" in req.messages[0]["content"]
    assert req.prompt_version == "draft-v2"


def test_notes_add_edit_delete_own_only(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None)
    with session_scope() as db:
        ann, ravi, boss = _member(db, "Ann"), _member(db, "Ravi"), _member(db, "Boss", "admin")
        n = notes_service.add_note(db, pid, "  Called; wants a quote.  ", actor=Actor.of_member(ann), member=ann)
        assert n.body == "Called; wants a quote." and n.author == "Ann" and n.can_edit
        assert notes_service.list_notes(db, pid, ravi)[0].can_edit is False
        with pytest.raises(DomainError) as e:
            notes_service.edit_note(db, n.id, "hijack", member=ravi)
        assert e.value.code == "not_owner"
        with pytest.raises(DomainError):
            notes_service.delete_note(db, n.id, member=boss)  # admins do not rewrite other people's notes
        edited = notes_service.edit_note(db, n.id, "Called; wants a quote by Friday.", member=ann)
        assert edited.body.endswith("Friday.")
        with pytest.raises(DomainError):
            notes_service.add_note(db, pid, "   ", actor=Actor.of_member(ann))
        assert notes_service.delete_note(db, n.id, member=ann) == pid
        assert notes_service.list_notes(db, pid) == []


def test_old_single_notes_field_style_writes_become_entries(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None, notes="Met at expo")
    with session_scope() as db:
        assert [n.body for n in notes_service.list_notes(db, pid)] == ["Met at expo"]
        prospects.update(db, pid, notes="Met at expo")  # same text again: not duplicated
        prospects.update(db, pid, notes="Call after Diwali", actor=Actor("Ravi", None))
        assert [n.body for n in notes_service.list_notes(db, pid)] == ["Call after Diwali", "Met at expo"]
        assert db.get(Prospect, pid).notes is None  # the legacy column is no longer written


def test_migrated_note_without_author_is_editable_by_admins_only(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None)
    with session_scope() as db:
        ann, boss = _member(db, "Ann"), _member(db, "Boss", "admin")
        row = ProspectNote(prospect_id=pid, body="old notes", author="team", source="migrated")
        db.add(row)
        db.flush()
        with pytest.raises(DomainError):
            notes_service.edit_note(db, row.id, "x", member=ann)
        assert notes_service.edit_note(db, row.id, "old notes, checked", member=boss).body == "old notes, checked"


def test_sheet_sync_cannot_touch_our_notes(db_url: str) -> None:
    """The sheet writes ``prospects.sheet_notes`` only; our entries live in another table and stay."""
    pid = make_prospect(website=None, linkedin=None, notes="Our own words")
    with session_scope() as db:
        p = db.get(Prospect, pid)
        p.sheet_notes = "Sheet: follow up in Jan"  # what a sheet sync does for the notes cell
        db.flush()
        assert [n.body for n in notes_service.list_notes(db, pid)] == ["Our own words"]

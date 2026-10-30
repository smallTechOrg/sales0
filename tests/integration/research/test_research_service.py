"""Prospect research service: four sources, one version per run, human edits as versions, drafting reads the latest."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import CompanyBrief, LlmCall, ProspectDoc, TeamMember, TimelineEvent, WorkerTask
from sales0.errors import DomainError
from sales0.services.crm.actor import Actor
from sales0.services.research import docs, linkedin_page

from sales0.services.research import prospect as research
from tests.integration.research.conftest import NEWS, SITE, make_prospect, mock_fetcher, responder
from tests.support.fake_llm import FakeLLMClient

ANN = Actor("Ann Rao")


def _texts(llm: FakeLLMClient, tool: str) -> list[str]:
    out = []
    for req in llm.requests:
        if req.tool_name != tool:
            continue
        content = req.messages[0]["content"]
        out.append(content if isinstance(content, str) else "".join(b["text"] for b in content))
    return out


def test_research_uses_website_own_data_and_web_search_and_stores_a_version(db_url: str, web_search_on: None) -> None:
    pid = make_prospect(notes="Met Priya at the Pune expo; wants a December slot.")
    llm = FakeLLMClient(responder=responder())
    hits: list[str] = []
    with session_scope() as db:
        doc, note = research.research_prospect(db, llm, None, pid, actor=ANN, queue_linkedin=False, fetcher=mock_fetcher(hits=hits))
    assert doc.version == 1 and doc.origin == "ai" and doc.status == "done" and doc.actor == "Ann Rao"
    assert doc.web_search_requests == 2 and doc.cost_usd_micros > 20_000
    kinds = [(s.kind, s.url) for s in doc.sources]
    assert ("website", SITE) in kinds and ("web_search", NEWS) in kinds and kinds[-1][0] == "own_data"
    assert doc.inputs["web_search"] == "done" and doc.inputs["pages_fetched"] == 1
    assert SITE in hits  # the website really was read through the fetcher
    step_prompts = _texts(llm, "research_step")
    assert "<our_records>" in step_prompts[0] and "Pune expo" in step_prompts[0]  # our notes, quoted as records
    assert 'kind="web_search"' in step_prompts[0] and "Mumbai hub" in step_prompts[0]
    assert "Invented fact" not in step_prompts[0]  # a finding citing a URL the search never returned is dropped
    web_req = next(r for r in llm.requests if r.tool_name == "record_web_findings")
    assert web_req.server_tools == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    assert web_req.tool_choice == "auto" and web_req.cache_system is False
    with session_scope() as db:
        rows = db.scalars(select(LlmCall).order_by(LlmCall.id)).all()
        assert [r.prompt_version for r in rows] == ["research-web-v2", "research-v3", "research-v3", "research-synth-v1"]
        assert rows[0].web_search_requests == 2 and sum(r.cost_usd_micros for r in rows) == doc.cost_usd_micros
        assert db.scalar(select(func.count(CompanyBrief.id)).where(CompanyBrief.prospect_id == pid)) == 1
        assert db.scalar(select(TimelineEvent.body).where(TimelineEvent.kind == "research")) == "Research version 1 (done)"


def test_web_search_setting_off_makes_no_search_request(db_url: str) -> None:
    pid = make_prospect()
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        doc, _ = research.research_prospect(db, llm, None, pid, actor=ANN, queue_linkedin=False, fetcher=mock_fetcher())
    assert all(r.tool_name != "record_web_findings" for r in llm.requests)
    assert doc.inputs["web_search"] == "off" and doc.web_search_requests == 0
    assert all(s.kind != "web_search" for s in doc.sources)


def test_no_website_still_researches_from_own_data_in_two_calls(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None)
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        doc, note = research.research_prospect(db, llm, None, pid, actor=ANN)
    assert [r.tool_name for r in llm.requests] == ["research_step", "record_research"] and doc.status == "done"
    assert "No LinkedIn company page" in note
    assert isinstance(llm.requests[0].messages[0]["content"], str)  # one-shot: no cache write
    assert llm.requests[0].model is None and llm.requests[1].model == "claude-sonnet-5-5"  # only the final brief uses Sonnet


def test_research_queues_one_company_page_task_and_uses_the_fetched_text_next_time(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_WORKER_TOKEN", "w" * 40)
    reset_settings()
    pid = make_prospect()
    llm = FakeLLMClient(responder=responder(fetch_first=False))
    with session_scope() as db:
        _, note = research.research_prospect(db, llm, None, pid, actor=ANN, fetcher=mock_fetcher())
        assert "queued for the laptop worker" in note
        research.research_prospect(db, llm, None, pid, actor=ANN, fetcher=mock_fetcher())  # no second task while pending
        tasks = db.scalars(select(WorkerTask)).all()
        assert len(tasks) == 1 and tasks[0].target_url == "https://www.linkedin.com/company/acme-logistics/"
        assert linkedin_page.status_for(db, pid, "https://www.linkedin.com/company/acme-logistics/").state == "queued"
        tasks[0].status, tasks[0].result_text = "done", "Acme Logistics | 850 followers | We move vaccines safely."
        tasks[0].result_url = "https://www.linkedin.com/company/acme-logistics/"
        db.flush()
        doc, _ = research.research_prospect(db, llm, None, pid, actor=ANN, fetcher=mock_fetcher())
    last = _texts(llm, "research_step")[-1]
    assert 'kind="linkedin_page"' in last and "We move vaccines safely." in last
    assert doc.inputs["linkedin_page"] is None or isinstance(doc.inputs["linkedin_page"], str)


def test_human_edit_is_a_new_version_and_drafting_reads_it(db_url: str) -> None:
    pid = make_prospect(website=None, linkedin=None)
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        ravi = TeamMember(name="Ravi", email="ravi@smalltech.example", role="member", active=True)
        db.add(ravi)
        db.flush()
        research.research_prospect(db, llm, None, pid, actor=ANN, queue_linkedin=False)
        edited = research.edit_research(
            db, pid, actor=Actor("Ravi", ravi.id), what_they_do="Cold-chain freight; also runs a training academy",
            personalisation_hooks="- their academy\n- vaccine logistics", sources=f"{NEWS}\n",
        )  # fmt: skip
        assert edited.version == 2 and edited.origin == "human" and edited.based_on_version == 1
        assert edited.origin_label == "Edited by Ravi" and edited.data["company_name"] == "Acme Logistics"
        assert [s.url for s in edited.sources if s.url] == [NEWS]
        brief, doc = research.brief_for_drafting(db, pid)
        assert doc.version == 2 and brief["personalisation_hooks"] == ["their academy", "vaccine logistics"]
        assert [v.version for v in docs.history(db, pid, "research")] == [2, 1]
        with pytest.raises(DomainError):
            research.edit_research(db, pid, actor=ANN, what_they_do=" ")
        with pytest.raises(DomainError):
            research.edit_research(db, pid, actor=ANN, what_they_do="x", sources="javascript:alert(1)")
        assert db.scalar(select(func.count(ProspectDoc.id))) == 2


def test_synthesis_stores_a_structured_record_and_drops_facts_with_unseen_sources(db_url: str) -> None:
    pid = make_prospect()
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        doc, _ = research.research_prospect(db, llm, None, pid, actor=ANN, queue_linkedin=False, fetcher=mock_fetcher())
    d = doc.data
    assert d["profile"]["industry"] == "Logistics" and d["enquiry"]["fit"] == "strong" and d["enquiry"]["urgency"] == "medium"
    claims = [f["claim"] for f in d["facts"]]
    assert "Runs cold-chain freight across India" in claims and "Asks for a workshop" in claims and "Invented claim" not in claims
    assert d["gaps"] == ["no budget stated"] and 0 < d["completeness"] <= 100
    assert doc.inputs["schema"] == 4 and doc.inputs["synthesis"]["ok"] is True and doc.inputs["synthesis"]["model"]


def test_synthesis_failure_keeps_the_loop_brief_and_leaves_it_pending(db_url: str) -> None:
    from sales0.llm import LLMTransientError

    pid = make_prospect()
    base = responder()

    def answer(req):  # type: ignore[no-untyped-def]
        if req.tool_name == "record_research":
            raise LLMTransientError("down")
        return base(req)

    llm = FakeLLMClient(responder=answer)
    with session_scope() as db:
        doc, _ = research.research_prospect(db, llm, None, pid, actor=ANN, queue_linkedin=False, fetcher=mock_fetcher())
        assert doc.status == "done" and "profile" in doc.data and doc.data["profile"] is None
        assert doc.inputs["schema"] == 3 and doc.inputs["synthesis"]["ok"] is False
        assert db.scalars(select(LlmCall).where(LlmCall.status == "error")).first() is not None


def test_synthesis_can_be_switched_off(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_RESEARCH_SYNTHESIS", "false")
    reset_settings()
    pid = make_prospect()
    llm = FakeLLMClient(responder=responder())
    with session_scope() as db:
        doc, _ = research.research_prospect(db, llm, None, pid, actor=ANN, queue_linkedin=False, fetcher=mock_fetcher())
    assert all(r.tool_name != "record_research" for r in llm.requests) and doc.inputs["schema"] == 3
    reset_settings()

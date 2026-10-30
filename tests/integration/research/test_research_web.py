"""Prospect page: Research / Account summary / Our notes cards and routes; compose reuses the latest research."""

from __future__ import annotations

import re
from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import OutreachMessage, ProspectNote
from sales0.services.crm.actor import Actor
from sales0.services.research import summary as summary_service
from tests.integration.research.conftest import HX, SITE, make_prospect
from tests.support.fake_llm import FakeLLMClient

JSON = {"Accept": "application/json"}


def test_prospect_page_shows_the_research_and_summary_cards_empty(web: TestClient) -> None:
    pid = make_prospect()
    html = web.get(f"/pipeline/prospects/{pid}").text
    for hook in ('id="research-card"', 'id="summary-card"', 'id="btn-research"', 'id="btn-summary"',
                 'id="research-empty"', 'id="summary-empty"', 'id="linkedin-page-status"', 'hx-indicator="#research-run-indicator"'):
        assert hook in html, hook
    assert 'data-state="unavailable"' in html  # no SALES0_WORKER_TOKEN here: says the worker is needed
    assert 'id="notes-card"' not in html  # "Our notes" was dropped from the company page
    assert 'id="ed-notes"' not in html  # the old single notes field is gone from Edit details


def test_research_button_runs_inside_the_request_and_renders_the_version(web: TestClient, llm: FakeLLMClient) -> None:
    pid = make_prospect()
    resp = web.post(f"/pipeline/prospects/{pid}/research", headers=HX)
    assert resp.status_code == 200, resp.text
    assert 'hx-swap-oob="outerHTML:#research-card"' in resp.text and "version 1" in resp.text
    assert "Cold-chain freight across India" in resp.text and f'href="{SITE}"' in resp.text
    assert "AI-generated" in resp.text and 'id="cost-badge"' in resp.text or "cost" in resp.text
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert 'data-version="1"' in page and "Re-run research" in page


def test_research_edit_and_history(web: TestClient) -> None:
    pid = make_prospect()
    web.post(f"/pipeline/prospects/{pid}/research", headers=HX)
    resp = web.post(f"/pipeline/prospects/{pid}/research/edit", headers=HX, data={
        "what_they_do": "Freight + a training academy", "personalisation_hooks": "academy\nvaccines", "sources": SITE,
    })  # fmt: skip
    assert resp.status_code == 200 and "Research saved as version 2" in unescape(resp.text)
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert "Human-edited · v2" in page and 'id="research-history"' in page and 'id="research-v1"' in page
    assert "Freight + a training academy" in page
    bad = web.post(f"/pipeline/prospects/{pid}/research/edit", data={"what_they_do": ""}, follow_redirects=False)
    assert bad.status_code == 303  # plain form: red flash back on the page
    data = web.post(f"/pipeline/prospects/{pid}/research/edit", headers=JSON, data={"what_they_do": "x"}).json()
    assert data["data"]["version"] == 3 and data["data"]["origin"] == "human"


def test_summary_refresh_and_edit_routes(web: TestClient) -> None:
    pid = make_prospect()
    resp = web.post(f"/pipeline/prospects/{pid}/summary", headers=HX)
    assert resp.status_code == 200 and "Account summary refreshed - version 1." in unescape(resp.text)
    assert "Who they are: Acme Logistics" in unescape(resp.text) and "AI-generated · v1" in resp.text
    edit = web.post(f"/pipeline/prospects/{pid}/summary/edit", headers=HX, data={"body": "Where we stand: warm, call Friday."})
    assert "Summary saved as version 2." in unescape(edit.text) and "Human-edited · v2" in edit.text
    page = unescape(web.get(f"/pipeline/prospects/{pid}").text)
    assert "Where we stand: warm, call Friday." in page and 'id="summary-v1"' in page


def test_notes_routes_add_edit_delete(web: TestClient) -> None:
    pid = make_prospect()
    added = web.post(f"/pipeline/prospects/{pid}/notes", headers=HX, data={"body": "Asked for a December slot"})
    assert added.status_code == 200 and "Note added." in added.text
    with session_scope() as db:
        nid = db.scalar(select(ProspectNote.id))
    assert web.post(f"/pipeline/notes/{nid}/edit", headers=HX, data={"body": "Asked for 9 Dec"}).status_code == 200
    with session_scope() as db:
        assert db.get(ProspectNote, nid).body == "Asked for 9 Dec"
    assert web.post(f"/pipeline/notes/{nid}/delete", headers=HX).status_code == 200
    with session_scope() as db:
        assert db.get(ProspectNote, nid) is None
    gone = web.post("/pipeline/notes/999/delete", headers=HX)  # htmx errors are a 200 toast
    assert gone.headers.get("HX-Retarget") == "#toasts" and "no longer exists" in gone.text
    assert web.post("/pipeline/notes/999/delete", headers=JSON).status_code == 404
    empty = web.post(f"/pipeline/prospects/{pid}/notes", headers=JSON, data={"body": " "})
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "note_invalid"


def test_compose_prefills_the_latest_research_and_the_draft_uses_it_plus_the_summary(web: TestClient, llm: FakeLLMClient) -> None:
    pid = make_prospect()
    web.post(f"/pipeline/prospects/{pid}/research", headers=HX)
    web.post(f"/pipeline/prospects/{pid}/research/edit", headers=HX, data={
        "what_they_do": "Freight", "personalisation_hooks": "their vaccine cold chain",
    })  # fmt: skip
    with session_scope() as db:
        summary_service.edit_summary(db, pid, "Where we stand: met at the expo.", actor=Actor("Boss"))
    compose = web.get(f"/outreach/compose?prospect_id={pid}").text
    assert 'name="research_doc_id"' in compose and "(version 2, Edited by" in unescape(compose)
    doc_id = re.search(r'name="research_doc_id" value="(\d+)"', compose).group(1)
    assert f'value="{SITE}"' in compose  # the company website is prefilled now
    resp = web.post("/outreach/draft", headers=HX, data={
        "to": "priya@acme.example", "company": "Acme Logistics", "contact_name": "Priya", "requirement": "GenAI workshop",
        "prospect_id": str(pid), "research_doc_id": doc_id,
    })  # fmt: skip
    assert resp.status_code == 204, resp.text
    req = [r for r in llm.requests if r.tool_name == "write_email"][-1]
    user = req.messages[0]["content"]
    assert "their vaccine cold chain" in user and "met at the expo" in user
    with session_scope() as db:
        msg = db.scalar(select(OutreachMessage))
        assert msg.prospect_id == pid and "their vaccine cold chain" in (msg.brief_json or "")


def test_compose_research_button_saves_a_prospect_version(web: TestClient) -> None:
    pid = make_prospect()
    resp = web.post("/outreach/research", headers=HX, data={"company": "Acme Logistics", "prospect_id": str(pid)})
    assert resp.status_code == 200 and 'name="research_doc_id"' in resp.text
    assert "saved as version 1 on the prospect" in unescape(resp.text)
    assert 'data-version="1"' in web.get(f"/pipeline/prospects/{pid}").text


def test_compose_research_without_a_prospect_is_unchanged(web: TestClient) -> None:
    resp = web.post("/outreach/research", headers=HX, data={"company": "Acme Logistics", "website": ""})
    assert resp.status_code == 200 and 'name="brief_id"' in resp.text

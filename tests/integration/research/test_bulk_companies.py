"""Bulk research from the Companies page: tick companies, one chunk per click, a Continue form for the rest."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Prospect, ProspectDoc
from sales0.services.crm.actor import Actor
from sales0.services.research import auto
from sales0.services.research import prospect as research
from sales0.web.routes import contacts as contacts_routes
from tests.integration.research.conftest import HX, make_prospect, mock_fetcher
from tests.support.fake_llm import FakeLLMClient


@pytest.fixture
def bulk(web: TestClient, llm: FakeLLMClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(auto, "default_fetcher", lambda: mock_fetcher())
    web.app.dependency_overrides[contacts_routes.get_llm] = lambda: llm  # type: ignore[attr-defined]
    return web


def _company_of(pid: int) -> int:
    with session_scope() as db:
        return int(db.get(Prospect, pid).company_id)


def _versions(pid: int) -> int:
    with session_scope() as db:
        return len(db.scalars(select(ProspectDoc).where(ProspectDoc.prospect_id == pid, ProspectDoc.doc_type == "research")).all())


def test_companies_page_has_checkboxes_and_the_bulk_button(bulk: TestClient) -> None:
    pid = make_prospect()
    html = bulk.get("/companies").text
    assert 'id="btn-research-selected"' in html and f'name="company_ids" value="{_company_of(pid)}"' in html


def test_research_selected_researches_each_company_with_a_fresh_version(bulk: TestClient) -> None:
    a, b = make_prospect(company="Acme Logistics"), make_prospect(name="Ravi", company="Beta Foods", website=None, linkedin=None)
    resp = bulk.post("/companies/research", headers=HX, data={"company_ids": [_company_of(a), _company_of(b)]})
    assert resp.status_code == 200 and "Researched 2 companies" in resp.text and 'id="research-bulk-summary"' in resp.text
    assert (_versions(a), _versions(b)) == (1, 1)
    bulk.post("/companies/research", headers=HX, data={"company_ids": [_company_of(a)]})
    assert _versions(a) == 2  # selected = research again, even when up to date


def test_chunking_returns_a_continue_form_for_the_rest(bulk: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_RESEARCH_BULK_CHUNK", "1")
    reset_settings()
    a, b = make_prospect(company="Acme Logistics"), make_prospect(name="Ravi", company="Beta Foods", website=None, linkedin=None)
    cids = [_company_of(a), _company_of(b)]
    first = bulk.post("/companies/research", headers=HX, data={"company_ids": cids}).text
    assert "Continue with 1 more" in first and f'value="{cids[1]}"' in first and (_versions(a), _versions(b)) == (1, 0)
    second = bulk.post("/companies/research", headers=HX, data={"company_ids": re.findall(r'name="company_ids" value="(\d+)"', first)}).text
    assert "Continue" not in second and _versions(b) == 1
    reset_settings()


def test_hand_edited_and_nothing_selected_are_handled(bulk: TestClient) -> None:
    pid = make_prospect()
    with session_scope() as db:
        research.edit_research(db, pid, actor=Actor("Ann"), what_they_do="Edited by hand")
    resp = bulk.post("/companies/research", headers=HX, data={"company_ids": [_company_of(pid)]})
    assert "edited by hand" in resp.text and _versions(pid) == 1
    assert bulk.post("/companies/research", headers={**HX, "Accept": "application/json"}, data={}).status_code == 400
    js = bulk.post("/companies/research", headers={"Accept": "application/json"}, data={"company_ids": [_company_of(pid)]}).json()
    assert js["ok"] is True and js["data"]["skipped"]

"""Prospect research / account summary / notes / company-page task fixtures. Fake LLM + fake fetcher only."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from sales0.agent.research import SafeFetcher
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.llm.client import LLMResponse, StructuredRequest
from sales0.services.crm import prospects
from sales0.web.app import create_app
from sales0.web.routes import outreach as outreach_routes
from sales0.web.routes import prospect_research
from tests.support.fake_llm import FakeLLMClient, make_response

HX = {"HX-Request": "true"}
SITE = "https://www.acme-logistics.test/"
PAGES = {
    "/": '<title>Acme Logistics</title><p>Acme Logistics runs cold-chain freight across India.</p><a href="/about">About</a>',
    "/about": "<title>About</title><p>850 employees in Pune and Mumbai.</p>",
}
NEWS = "https://news.example/acme-expands"


def mock_fetcher(pages: dict[str, str] = PAGES, hits: list[str] | None = None) -> SafeFetcher:
    seen = hits if hits is not None else []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path in pages:
            return httpx.Response(200, content=pages[req.url.path].encode(), headers={"content-type": "text/html"})
        return httpx.Response(404, content=b"nf", headers={"content-type": "text/html"})

    return SafeFetcher(resolver=lambda h: ["93.184.216.34"], transport=httpx.MockTransport(handler), respect_robots=False)


def brief(**kw: Any) -> dict[str, Any]:
    b = {
        "company_name": "Acme Logistics", "what_they_do": "Cold-chain freight across India",
        "size_hints": "850 employees", "ai_training_needs": ["GenAI for operations"],
        "suggested_offering": "Workshops", "matched_offering_name": None,
        "personalisation_hooks": ["cold-chain freight", "850 employees in Pune"], "sources": [SITE, NEWS],
    }  # fmt: skip
    b.update(kw)
    return b


SUMMARY = {
    "who_they_are": "Acme Logistics, cold-chain freight, Pune.", "what_they_want": "A GenAI workshop for 40 managers.",
    "where_we_stand": "New prospect, not contacted yet.", "characteristics": ["Training provider"],
    "open_follow_ups": [], "next_best_step": "Email Priya about the workshop.",
}  # fmt: skip


def web_answer(req: StructuredRequest) -> LLMResponse:
    base = make_response({"summary": "Acme expanded to Mumbai.", "findings": [
        {"fact": "Acme Logistics opened a Mumbai hub (Oct 2026)", "url": NEWS, "title": "Acme expands"},
        {"fact": "Invented fact", "url": "https://not-returned.example/"},
    ]})  # fmt: skip
    return base.model_copy(update={
        "web_search_requests": 2, "cost_usd_micros": base.cost_usd_micros + 20_000,
        "web_results": [{"url": NEWS, "title": "Acme expands", "page_age": None}],
    })  # fmt: skip


def responder(*, fetch_first: bool = True, summary: dict[str, Any] | None = None) -> Callable[[StructuredRequest], Any]:
    state = {"fetched": False}

    def answer(req: StructuredRequest) -> Any:
        if req.tool_name == "record_web_findings":
            return web_answer(req)
        if req.tool_name == "record_account_summary":
            return summary or SUMMARY
        if req.tool_name == "research_step":
            allow = "fetch_url" in req.input_schema["properties"]["action"]["enum"]
            if fetch_first and allow and not state["fetched"]:
                state["fetched"] = True
                return {"action": "fetch_url", "url": SITE, "thought": "home page"}
            return {"action": "finish", "brief": brief()}
        if req.tool_name == "write_email":
            return {"subject": "AI workshops for Acme Logistics",
                    "body": "Hello Priya,\n\nAcme Logistics and cold-chain freight: we run GenAI workshops.\n\nBest regards,\nBoss"}
        raise AssertionError(f"unexpected tool {req.tool_name}")

    return answer


@pytest.fixture
def llm() -> FakeLLMClient:
    return FakeLLMClient(responder=responder())


@pytest.fixture
def web(db_url: str, llm: FakeLLMClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(prospect_research, "_fetcher", lambda: mock_fetcher())
    monkeypatch.setattr(outreach_routes, "_research_fetcher", lambda: mock_fetcher())
    app = create_app()
    app.dependency_overrides[prospect_research.get_llm] = lambda: llm
    app.dependency_overrides[outreach_routes.get_llm] = lambda: llm
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(db_url: str) -> Iterator[Session]:
    with session_scope() as session:
        yield session


def make_prospect(
    *, name: str = "Priya Menon", company: str = "Acme Logistics", website: str | None = SITE,
    linkedin: str | None = "https://www.linkedin.com/company/acme-logistics/", notes: str | None = None,
) -> int:
    with session_scope() as db:
        v = prospects.create_manual(db, name=name, company=company, email="priya@acme.example", website=website, notes=notes)
        if linkedin:
            prospects.update(db, v.id, company_linkedin_url=linkedin)
        return v.id


@pytest.fixture
def web_search_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_RESEARCH_WEB_SEARCH", "true")
    reset_settings()

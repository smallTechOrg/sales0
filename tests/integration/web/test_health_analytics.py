"""q24: the Health page (every moving part and what to do) and Analytics (funnel, time in stage, close reasons,
outreach activity, the AI read of what worked)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any

from fastapi.testclient import TestClient

from sales0 import llm as llm_module
from sales0.db.engine import session_scope
from sales0.llm.client import StructuredRequest
from sales0.services import analytics
from sales0.services.crm import lifecycle, prospects, timeline
from sales0.services.crm.actor import Actor
from tests.support.fake_llm import FakeLLMClient

ANA = Actor("Ana", None)


def _seed() -> None:
    with session_scope() as db:
        won = prospects.create_manual(db, name="Asha Rao", company="Won Co")
        lost = prospects.create_manual(db, name="Bo Li", company="Lost Co")
        prospects.mark_contacted(db, won.id, when=datetime.now(UTC) - timedelta(days=10))
        timeline.add_note(db, subject_type="prospect", subject_id=won.id, body="Sent the AI workshop deck after the call", actor=ANA)
        lifecycle.mark_lead(db, won.company_id)
        lifecycle.mark_client(db, won.company_id)
        lifecycle.go_off_ramp(db, lost.company_id, "not_interested", reason="budget frozen")


def test_health_page_lists_every_part(client: TestClient) -> None:
    html = unescape(client.get("/health").text)
    for key in ("replies", "worker", "linkedin", "ai", "runs", "sheet", "auto_status", "database"):
        assert f'id="health-{key}"' in html
    assert 'id="health-summary"' in html and "Schema up to date" in html


def test_analytics_numbers(client: TestClient) -> None:
    _seed()
    with session_scope() as db:
        r = analytics.report(db, period="all")
    steps = {s.key: s.count for s in r.funnel}
    assert steps == {"pipeline": 2, "contacted": 1, "lead": 1, "client": 1}
    assert any(counts.get("not_interested") for _m, counts in r.close_by_month)
    assert r.close_reasons == [("budget frozen", 1)]
    html = unescape(client.get("/metrics/analytics?period=all").text)
    assert 'id="an-funnel"' in html and "Became a client" in html and "budget frozen" in html and 'id="insights-empty"' in html


def test_the_ai_read_uses_the_history_and_is_stored(client: TestClient) -> None:
    _seed()
    seen: list[str] = []

    def answer(req: StructuredRequest) -> Any:
        assert req.tool_name == "record_insights"
        seen.append(req.messages[0]["content"])
        return {"worked": [{"point": "Fast follow-up with a deck", "evidence": "Won Co got the deck after the call"}],
                "did_not_work": [{"point": "No budget check", "evidence": "Lost Co: budget frozen"}],
                "patterns": ["Workshops close faster"], "next_steps": ["Ask about budget on the first call"]}

    llm_module.set_llm_client(FakeLLMClient(responder=answer))
    try:
        r = client.post("/metrics/analytics/insights", follow_redirects=False)
    finally:
        llm_module.set_llm_client(None)
    assert r.status_code == 303
    assert "Won Co" in seen[0] and "Lost Co" in seen[0] and "Sent the AI workshop deck" in seen[0]
    html = unescape(client.get("/metrics/analytics").text)
    assert "Fast follow-up with a deck" in html and "Ask about budget on the first call" in html

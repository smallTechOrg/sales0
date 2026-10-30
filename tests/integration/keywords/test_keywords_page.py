"""Keywords page: performance tiers + downstream outcomes, easy manual editing (inline edit, toggle, bulk add, bulk
pause / activate / move / delete) and AI keyword ideas on the strong model, reviewed and added in bulk."""

from __future__ import annotations

import re
from collections.abc import Iterator
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.agent import keyword_ideas
from sales0.db.engine import session_scope
from sales0.db.models import Company, Enquiry, Keyword, LlmCall
from sales0.services import keyword_insights as insights
from sales0.services import keywords as kw
from sales0.services import offerings as offerings_service
from sales0.services.views import OfferingInput
from sales0.web.app import create_app
from sales0.web.routes import keywords as keywords_routes
from tests.integration.web.conftest import HX, make_enquiry
from tests.support.fake_llm import FakeLLMClient

JSON = {"Accept": "application/json"}


def ideas_answer(**over: Any) -> dict[str, Any]:
    return {
        "suggestions": [
            {"phrase": "need corporate GenAI trainer", "offering": "Workshops", "kind": "variant", "based_on": "ai workshop",
             "reason": "Buyers say 'need a trainer'.", "evidence": [1, 99]},
            {"phrase": "AI Workshop", "offering": "Workshops", "kind": "new", "reason": "dup of existing", "evidence": []},
            {"phrase": "x", "offering": "", "kind": "new", "reason": "too short", "evidence": []},
            {"phrase": "need corporate genai trainer", "offering": "", "kind": "new", "reason": "dup within batch", "evidence": []},
            {"phrase": "llm upskilling program employees", "offering": "Nonexistent", "kind": "new", "reason": "adjacent need", "evidence": []},
        ],
        "retire": [
            {"keyword": "dead phrase", "action": "reword", "reason": "only vendors", "better_phrase": "hire ai trainer"},
            {"keyword": "not a keyword we have", "action": "pause", "reason": "x"},
        ],
        "avoid": ["'we offer'", "course promotions"],
        **over,
    }


@pytest.fixture
def llm() -> FakeLLMClient:
    return FakeLLMClient(responder=lambda req: ideas_answer())


@pytest.fixture
def web(db_url: str, llm: FakeLLMClient) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[keywords_routes.get_llm] = lambda: llm
    with TestClient(app) as c:
        yield c


def _offering(name: str = "Workshops") -> int:
    with session_scope() as db:
        return offerings_service.create_offering(db, OfferingInput(name=name, group_name="Training", description="Hands-on GenAI workshops")).id


def _keyword(text: str, *, scanned: int = 0, relevant: int = 0, offering_id: int | None = None, active: bool = True) -> int:
    with session_scope() as db:
        v = kw.add_keyword(db, text, offering_id=offering_id)
        row = db.get(Keyword, v.id)
        row.scanned_total, row.relevant_total, row.active = scanned, relevant, active
        return v.id


def _seed() -> dict[str, int]:
    oid = _offering()
    ids = {
        "ai workshop": _keyword("ai workshop", scanned=120, relevant=15, offering_id=oid),
        "genai training": _keyword("genai training", scanned=50, relevant=1),
        "brand new": _keyword("brand new", scanned=5),
        "meh phrase": _keyword("meh phrase", scanned=40),
        "dead phrase": _keyword("dead phrase", scanned=200),
    }
    won = make_enquiry("P1", keyword="ai workshop", review_status="accepted", ai_verdict="genuine", deal_status="won")
    make_enquiry("P2", keyword="AI Workshop", review_status="accepted", ai_verdict="genuine", deal_status="contacted")
    make_enquiry("P3", keyword="dead phrase", review_status="discarded", ai_verdict="discard")
    with session_scope() as db:
        co = Company(name="Won Co", name_key="won co", stage="client")
        db.add(co)
        db.flush()
        db.get(Enquiry, won).company_id = co.id
    return {"offering": oid, **ids}


# ---------------------------------------------------------------------- tiers and outcomes
def test_tiers_and_downstream_outcomes(db_url: str) -> None:
    ids = _seed()
    with session_scope() as db:
        stats = {s.text: s for s in insights.keyword_stats(db)}
    assert {t: stats[t].tier for t in stats} == {
        "ai workshop": "winner", "genai training": "promising", "brand new": "new", "meh phrase": "underperforming",
        "dead phrase": "dead",
    }
    w = stats["ai workshop"]  # the hit with a different-case text snapshot still counts (matched by text)
    assert (w.found, w.accepted, w.contacted, w.leads, w.won) == (2, 2, 2, 1, 1) and w.offering_id == ids["offering"]
    assert stats["dead phrase"].found == 1 and stats["dead phrase"].accepted == 0
    assert insights.tier_of(scanned=10, relevant=0, accepted=2) == "winner"  # accepted enquiries win even on low volume


def test_grouping_by_tier_offering_and_outcome(db_url: str) -> None:
    _seed()
    with session_scope() as db:
        stats = insights.keyword_stats(db)
    assert [g.key for g in insights.grouped(stats, "tier")] == ["winner", "promising", "new", "underperforming", "dead"]
    by_offering = insights.grouped(stats, "offering")
    assert [g.label for g in by_offering] == ["Workshops", "No offering"] and len(by_offering[1].keywords) == 4
    assert [(g.key, [k.text for k in g.keywords]) for g in insights.grouped(stats, "outcome")][0] == ("won", ["ai workshop"])
    rel = insights.filtered(stats, sort="relevant")
    assert [s.text for s in rel][:2] == ["ai workshop", "genai training"]
    assert [s.text for s in insights.filtered(stats, q="phrase", status="active", sort="text")] == ["dead phrase", "meh phrase"]


# ---------------------------------------------------------------------- manual editing
def test_bulk_add_reports_duplicates_and_invalid(db_url: str) -> None:
    oid = _offering()
    _keyword("ai workshop")
    with session_scope() as db:
        r = kw.bulk_add(db, ["  GenAI   bootcamp ", "AI WORKSHOP", "genai bootcamp", "x", ""], offering_id=oid)
        assert (r.added, r.duplicates, r.invalid) == (["GenAI bootcamp"], ["AI WORKSHOP", "genai bootcamp"], ["x"])
        assert db.scalar(select(Keyword).where(Keyword.text_key == "genai bootcamp")).offering_id == oid
        assert r.summary() == "Added 1 keyword · 2 already there · 1 skipped (2-100 characters)."


def test_page_renders_and_bulk_edits_round_trip(web: TestClient) -> None:
    ids = _seed()
    html = unescape(web.get("/keywords").text)
    assert 'href="/keywords"' in html and 'id="kw-ideas-form"' in html and 'data-select-all=".kw-pick"' in html
    assert "onclick" not in html and html.index("Winners") < html.index("Dead (no yield)")
    assert re.search(r'id="kw-%d"[^>]*data-tier="winner"' % ids["ai workshop"], html)
    frag = web.get("/keywords?view=offering&sort=relevant", headers={**HX, "HX-Target": "keyword-results"}).text
    assert frag.lstrip().startswith('<div id="keyword-results"') and "No offering" in frag
    back = "view=tier&sort=tier"

    added = web.post("/keywords/add", headers=HX, data={"texts": "hire genai trainer, ai workshop\nllm bootcamp", "offering_id": str(ids["offering"]), "back": back})
    assert "Added 2 keywords · 1 already there." in unescape(added.text) and 'hx-swap-oob="outerHTML:#keyword-results"' in added.text

    paused = web.post("/keywords/bulk", headers=HX, data={"keyword_ids": [ids["dead phrase"], ids["meh phrase"]], "action": "pause", "back": back})
    assert "Paused 2 keywords." in unescape(paused.text)
    moved = web.post("/keywords/bulk", headers=HX, data={"keyword_ids": [ids["brand new"]], "action": "move", "move_to": str(ids["offering"])})
    assert "Moved 1 keyword." in unescape(moved.text)
    edit = web.post(f"/keywords/{ids['genai training']}/edit", headers=HX, data={"text": "GenAI training for teams", "offering_id": ""})
    assert "Saved 'GenAI training for teams'." in unescape(edit.text)
    toggled = web.post(f"/keywords/{ids['dead phrase']}/toggle", headers=HX, data={"active": "true"})
    assert "'dead phrase' active." in unescape(toggled.text)
    with session_scope() as db:
        rows = {k.text: k for k in db.scalars(select(Keyword))}
        assert rows["meh phrase"].active is False and rows["dead phrase"].active is True
        assert rows["brand new"].offering_id == ids["offering"] and rows["GenAI training for teams"].offering_id is None
    gone = web.post("/keywords/bulk", headers=HX, data={"keyword_ids": [ids["brand new"]], "action": "delete"})
    assert "Removed 1 keyword." in unescape(gone.text)
    nothing = web.post("/keywords/bulk", headers={**HX, **JSON}, data={"action": "pause"})
    assert nothing.status_code == 400
    js = web.get("/keywords?tier=winner", headers=JSON).json()["data"]
    assert [r["text"] for r in js] == ["ai workshop"] and js[0]["won"] == 1


# ---------------------------------------------------------------------- AI ideas
def test_ideas_use_the_strong_model_and_code_enforces_the_rules(db_url: str, llm: FakeLLMClient) -> None:
    _seed()
    with session_scope() as db:
        result = keyword_ideas.generate(db, llm, focus="BFSI in Pune")
        call = db.scalar(select(LlmCall).order_by(LlmCall.id.desc()))
    req = llm.requests[-1]
    assert req.model == "claude-opus-5-5" and req.tool_name == "record_keyword_ideas" and req.feature == "suggest"
    prompt = req.messages[0]["content"]
    assert "ai workshop | Workshops | winner | 120 | 15 | 2 | 1" in prompt and "BFSI in Pune" in prompt
    assert "## Noise posts" in prompt and "[found by: dead phrase]" in prompt and "<post>" in prompt
    assert [i.phrase for i in result.ideas] == ["need corporate GenAI trainer", "llm upskilling program employees"]
    first, second = result.ideas
    assert first.offering_name == "Workshops" and len(first.evidence) == 1 and second.offering_id is None  # unknown offering dropped
    assert [(r.keyword, r.action, r.better_phrase) for r in result.retire] == [("dead phrase", "reword", "hire ai trainer")]
    assert result.dropped == 4 and result.avoid == ["'we offer'", "course promotions"]
    assert call.prompt_version == "keyword-ideas-v1" and call.status == "ok"


def test_review_list_then_add_ticked_ideas_and_pause_retired(web: TestClient) -> None:
    ids = _seed()
    html = unescape(web.post("/keywords/ideas", headers=HX, data={"focus": ""}).text)
    assert 'id="kw-apply-form"' in html and 'value="need corporate GenAI trainer"' in html and "Pause <strong>dead phrase</strong>" in html
    assert "try instead: \"hire ai trainer\"" in html and "4 proposals dropped" in html and "2 ideas ready to review." in html
    resp = web.post("/keywords/ideas/apply", headers=HX, data={
        "pick": ["0"], "phrase": ["need GenAI trainer (edited)", "llm upskilling program employees"],
        "idea_offering": [str(ids["offering"]), ""], "pause_ids": [ids["dead phrase"]],
    })
    assert "Added 1 keyword, paused 1." in unescape(resp.text)
    with session_scope() as db:
        rows = {k.text: k for k in db.scalars(select(Keyword))}
        assert rows["need GenAI trainer (edited)"].offering_id == ids["offering"] and rows["dead phrase"].active is False
        assert "llm upskilling program employees" not in rows  # not ticked
    assert web.post("/keywords/ideas/apply", headers={**HX, **JSON}, data={}).status_code == 400


def test_ideas_failures_are_clear(web: TestClient, llm: FakeLLMClient) -> None:
    from sales0.llm import LLMTransientError

    def down(req: Any) -> Any:
        raise LLMTransientError("down")

    llm._responder = down  # noqa: SLF001
    resp = web.post("/keywords/ideas", headers={**HX, **JSON}, data={})
    assert resp.status_code == 502 and "unavailable" in resp.text

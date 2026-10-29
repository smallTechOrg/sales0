"""Inbox feedback A1 through the real discovery job (scripted searcher + FakeLLMClient, real SQLite): one enquiry per
existing prospect per run reaches To review; the rest are auto-skipped and counted in the run summary."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, ScrapeRun
from sales0.jobs import discovery_job
from sales0.services import keywords, runs
from sales0.services.crm import prospects
from tests.integration.services.helpers import FakeSearcher, sdui_post, set_session_status
from tests.support.fake_llm import FakeLLMClient, triage_answer

TEXTS = [
    "Northwind Foods needs a GenAI productivity workshop for 60 sales staff in Chennai next month",
    "Northwind Foods is looking for Claude Code training for 20 engineers, online, December",
    "Northwind Foods wants an AI assistant built over its HR policies, vendors please reach out",
    "Quartz Insurance needs a partner to build a RAG assistant over policy PDFs, DM to discuss",
]


def company_llm() -> FakeLLMClient:
    def answer(req):  # type: ignore[no-untyped-def]
        text = str(req.messages[0]["content"])
        return triage_answer(company="Quartz Insurance" if "Quartz" in text else "Northwind Foods", contact_person=None)

    return FakeLLMClient(responder=answer)


def test_run_keeps_one_per_existing_prospect_and_counts_the_rest(db_url: str) -> None:
    set_session_status("connected")
    with session_scope() as db:
        keywords.add_keyword(db, "ai workshop")
        prospects.create_manual(db, name="Northwind Foods", company="Northwind Foods")
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=datetime.now(UTC))
        db.add(run)
        db.flush()
        run_id = run.id
    posts = [sdui_post(f"k{i}", t, slug=f"in/poster-{i}-fx", author=f"Poster {i}") for i, t in enumerate(TEXTS)]
    discovery_job.execute(run_id, llm=company_llm(), searcher_factory=FakeSearcher({"ai workshop": posts}),
                          sleep=lambda _s: None)
    with session_scope() as db:
        r = runs.get_run(db, run_id).run
        assert (r.status, r.posts_new, r.posts_relevant, r.posts_repeat_skipped) == ("succeeded", 4, 2, 2)
        assert "repeats from pipeline skipped 2" in (r.current_step or "")
        rows = db.scalars(select(Enquiry).order_by(Enquiry.id)).all()
        # triage runs concurrently: "first" is the first one persisted, so compare as a multiset
        assert sorted((e.company, e.review_status, e.discard_source or "") for e in rows) == [
            ("Northwind Foods", "discarded", "auto"),
            ("Northwind Foods", "discarded", "auto"),
            ("Northwind Foods", "pending_review", ""),
            ("Quartz Insurance", "pending_review", ""),
        ]

"""Inbox feedback A1: repeat enquiries from prospects already in the pipeline (real SQLite, synthetic data).

Within one scrape run the FIRST new enquiry that matches an existing prospect/company stays in To review, marked
"Already in pipeline"; further ones are auto-discarded by the system (no triage decision, so never a few-shot
example) and noted on the prospect's timeline. Accept joins the existing prospect instead of creating a duplicate.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.agent import TriageOutcome, TriageResult, load_candidates
from sales0.db.models import Enquiry, Prospect, ScrapeRun, TimelineEvent, TriageDecision
from sales0.jobs import discovery_job
from sales0.services import inbox
from sales0.services.crm import pipeline_match, prospects
from sales0.services.seen_posts import persist_triage, register_sighting, rescue
from tests.integration.services.helpers import sdui_post
from tests.support.fake_llm import make_response, triage_answer

KW = "corporate ai training"
TEXTS = [
    "Northwind Foods needs a GenAI productivity workshop for 60 sales staff in Chennai next month, proposals welcome",
    "Northwind Foods is also looking for a Claude Code training for its 20 engineers, online, December batch",
    "Northwind Foods wants an AI assistant built over its HR policies; vendors please reach out with proposals",
    "Northwind Foods: second batch of the GenAI workshop for the finance team in Chennai, January, need a partner",
]


def new_run(db: Session) -> int:
    run = ScrapeRun(trigger="manual", mode="live", status="running")
    db.add(run)
    db.flush()
    return run.id


def outcome(**overrides: object) -> TriageOutcome:
    answer = triage_answer(**{"company": "Northwind Foods", "contact_person": None, **overrides})
    return TriageOutcome(
        status="done", result=TriageResult.model_validate(answer), error=None,
        llm_responses=[make_response(answer)], fewshot_count=0, prompt_version="triage-v3",
    )


def enquiry_for(db: Session, i: int, run_id: int | None, *, slug: str | None = None, **answer: object):  # type: ignore[no-untyped-def]
    s = register_sighting(db, sdui_post(f"key{i}", TEXTS[i], slug=slug or f"in/poster-{i}-fx", author=f"Poster {i}"),
                          run_id or new_run(db), None, KW)
    persisted = persist_triage(db, s.seen_post_id, outcome(**answer), run_id=run_id, keyword_id=None)
    assert persisted.enquiry_id is not None
    return persisted, db.get(Enquiry, persisted.enquiry_id)


def existing_prospect(db: Session, **kw: object) -> Prospect:
    view = prospects.create_manual(db, name=str(kw.pop("name", "Northwind Foods")), **kw)  # type: ignore[arg-type]
    db.flush()
    p = db.get(Prospect, view.id)
    assert p is not None
    return p


def test_first_match_in_a_run_stays_marked_and_later_ones_are_auto_discarded(db: Session) -> None:
    p = existing_prospect(db, company="Northwind Foods")
    run_id = new_run(db)
    results = [enquiry_for(db, i, run_id) for i in range(3)]

    first = results[0][1]
    assert (first.review_status, first.discard_source) == ("pending_review", None)
    assert (first.pipeline_prospect_id, first.pipeline_match, first.pipeline_run_id) == (p.id, "company", run_id)
    assert results[0][0].repeat_skipped is False
    for n, (persisted, e) in enumerate(results[1:], start=1):
        assert persisted.repeat_skipped is True
        assert (e.review_status, e.discard_source, e.pipeline_prospect_id) == ("discarded", "auto", p.id)
        assert e.discard_reason == f"Repeat from existing prospect Northwind Foods (already {n} this run)"
        assert e.prospect_id is None and e.decided_at is not None
    # system decisions: no triage_decisions row, so nothing reaches the few-shot examples
    assert db.scalar(select(func.count(TriageDecision.id))) == 0
    assert load_candidates(db, exclude_enquiry_id=0) == []
    notes = db.scalars(
        select(TimelineEvent).where(TimelineEvent.subject_type == "prospect", TimelineEvent.subject_id == p.id,
                                    TimelineEvent.kind == "note")
    ).all()
    auto = [n for n in notes if (n.data or {}).get("auto_discard") == "repeat"]
    assert sorted(n.enquiry_id for n in auto) == sorted(e.id for _, e in results[1:])
    assert all(n.body and n.body.startswith("Repeat enquiry auto-skipped") for n in auto)


def test_a_new_run_lets_one_through_again(db: Session) -> None:
    existing_prospect(db, company="Northwind Foods")
    run_a, run_b = new_run(db), new_run(db)
    enquiry_for(db, 0, run_a)
    assert enquiry_for(db, 1, run_a)[1].discard_source == "auto"
    e = enquiry_for(db, 2, run_b)[1]
    assert (e.review_status, e.discard_source, e.pipeline_run_id) == ("pending_review", None, run_b)


def test_author_profile_match_wins_and_uses_the_prospect_name(db: Session) -> None:
    p = existing_prospect(db, name="Asha Rao", linkedin_url="https://www.linkedin.com/in/asha-rao-fx/")
    run_id = new_run(db)
    _, first = enquiry_for(db, 0, run_id, slug="in/asha-rao-fx", company=None)
    _, second = enquiry_for(db, 1, run_id, slug="in/asha-rao-fx", company=None)
    assert (first.pipeline_prospect_id, first.pipeline_match, first.review_status) == (p.id, "profile", "pending_review")
    assert second.discard_reason == "Repeat from existing prospect Asha Rao (already 1 this run)"


def test_company_email_domain_and_company_page_match(db: Session) -> None:
    from sales0.services.crm import companies

    acme = companies.get_or_create_row(db, "Helio Payments Pvt Ltd", website="https://www.heliopay.example/")
    assert acme is not None
    acme.linkedin_url = "https://www.linkedin.com/company/helio-payments-fx/"
    db.flush()
    run_id = new_run(db)
    _, by_mail = enquiry_for(db, 0, run_id, company=None, email="l-and-d@heliopay.example")
    assert (by_mail.pipeline_company_id, by_mail.pipeline_match, by_mail.pipeline_prospect_id) == (acme.id, "website", None)
    _, by_page = enquiry_for(db, 1, run_id, slug="company/helio-payments-fx", company=None)
    assert (by_page.pipeline_company_id, by_page.pipeline_match) == (acme.id, "company_page")
    assert by_page.discard_source == "auto"  # same company, second one this run
    _, free_mail = enquiry_for(db, 2, new_run(db), company=None, email="someone@gmail.com")
    assert free_mail.pipeline_match is None  # a free-mail domain is never a company key


def test_no_pipeline_match_changes_nothing(db: Session) -> None:
    run_id = new_run(db)
    for i in range(3):
        _, e = enquiry_for(db, i, run_id)
        assert (e.review_status, e.pipeline_match, e.discard_source) == ("pending_review", None, None)


def test_enquiry_discarded_prospects_do_not_count(db: Session) -> None:
    p = existing_prospect(db, company="Northwind Foods")
    p.status, p.closed_reason = "closed", "enquiry_discarded"
    db.flush()
    assert enquiry_for(db, 0, new_run(db))[1].pipeline_match is None


def test_outside_a_run_it_only_marks(db: Session) -> None:
    p = existing_prospect(db, company="Northwind Foods")
    a = enquiry_for(db, 0, None)[1]
    b = enquiry_for(db, 1, None)[1]
    assert [(e.review_status, e.pipeline_prospect_id) for e in (a, b)] == [("pending_review", p.id)] * 2


def test_accept_joins_the_existing_prospect_instead_of_a_duplicate(db: Session) -> None:
    p = existing_prospect(db, company="Northwind Foods")
    _, e = enquiry_for(db, 0, new_run(db))
    before = db.scalar(select(func.count(Prospect.id)))
    inbox.decide(db, [e.id], "accept")
    view = prospects.ensure_for_enquiry(db, e.id)
    assert view.id == p.id and db.get(Enquiry, e.id).prospect_id == p.id  # type: ignore[union-attr]
    assert db.scalar(select(func.count(Prospect.id))) == before


def test_accepting_an_auto_discarded_repeat_is_a_team_decision_and_joins_the_prospect(db: Session) -> None:
    p = existing_prospect(db, company="Northwind Foods")
    run_id = new_run(db)
    enquiry_for(db, 0, run_id)
    _, repeat = enquiry_for(db, 1, run_id)
    inbox.decide(db, [repeat.id], "accept")
    assert (repeat.review_status, repeat.discard_source) == ("accepted", None)
    assert db.scalar(select(func.count(TriageDecision.id))) == 1  # the human decision feeds few-shot
    assert prospects.ensure_for_enquiry(db, repeat.id).id == p.id


def test_card_shows_the_pipeline_link(db: Session) -> None:
    p = existing_prospect(db, company="Northwind Foods")
    _, e = enquiry_for(db, 0, new_run(db))
    card = inbox.get_card(db, e.id)
    assert (card.pipeline_name, card.pipeline_href, card.pipeline_match) == (
        "Northwind Foods", f"/pipeline/prospects/{p.id}", pipeline_match.MATCH_LABELS["company"]
    )


def test_rescue_marks_without_discarding(db: Session) -> None:
    existing_prospect(db, company="Northwind Foods")
    run_id = new_run(db)
    s = register_sighting(db, sdui_post("r1", TEXTS[0]), run_id, None, KW)
    persist_triage(db, s.seen_post_id, outcome(verdict="discard", poster_type="training_agency"), run_id=run_id, keyword_id=None)
    card = rescue(db, s.seen_post_id)
    e = db.get(Enquiry, card.id)
    assert e is not None and (e.review_status, e.pipeline_match) == ("pending_review", "company")


def test_summary_text_reports_repeats_only_when_there_are_some() -> None:
    run = ScrapeRun(posts_seen=5, posts_new=5, posts_relevant=2, posts_skipped_ai=1, posts_dupe=0, posts_repeat_skipped=2)
    assert discovery_job.summary_text(run).endswith("· repeats from pipeline skipped 2")
    run.posts_repeat_skipped = 0
    assert "repeats" not in discovery_job.summary_text(run)

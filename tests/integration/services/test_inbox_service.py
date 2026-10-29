"""Review inbox on real SQLite: tabs, sort, search, accept/discard/override, bulk, field edits."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.db.engine import reset_engine, session_scope
from sales0.db.models import Enquiry, LlmCall, StatusHistory, TriageDecision
from sales0.errors import DomainError
from sales0.services import inbox
from tests.integration.services.helpers import make_enquiry, make_run, set_triaged


@pytest.fixture
def seeded(db_url: str) -> dict[str, int]:
    """P1 genuine/high, P2+P3 discard, P9 discard, one failed, one still pending."""
    run_id = make_run()
    ids = {
        "P1": make_enquiry("Acme Logistics wants a training partner for a GenAI workshop in Pune", n=1, run_id=run_id),
        "P2": make_enquiry("SkillBridge is hiring freelance Generative AI trainers", n=2, run_id=run_id),
        "P3": make_enquiry("Need an individual freelance trainer, hourly rate", n=3, run_id=run_id),
        "P9": make_enquiry("I am a certified GenAI trainer open to freelance work", n=9, run_id=run_id),
        "FAIL": make_enquiry("Something the AI could not read", n=12, run_id=run_id),
        "PEND": make_enquiry("Not triaged yet", n=13, run_id=run_id),
    }
    set_triaged(ids["P1"], verdict="genuine", priority="high", offering_id=1, company="Acme Logistics")
    set_triaged(ids["P2"], verdict="discard", priority="low", offering_id=None, company="SkillBridge",
                reason="Agency recruiting freelance trainers, not a buyer")
    set_triaged(ids["P3"], verdict="discard", priority="low", offering_id=None, company=None)
    set_triaged(ids["P9"], verdict="discard", priority="low", offering_id=None, company=None)
    with session_scope() as db:
        failed = db.get(Enquiry, ids["FAIL"])
        assert failed is not None
        failed.triage_status, failed.triage_error = "failed", "LLM unavailable — retry later"
        db.add(LlmCall(feature="triage", model="m", enquiry_id=ids["P1"], input_tokens=1000, output_tokens=200,
                       cost_usd_micros=2000, latency_ms=5, status="ok"))
    return ids


def test_review_tab_pins_failed_then_genuine_then_priority_with_counts(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        page = inbox.list_enquiries(db, "review", None)
    order = [c.id for c in page.items]
    assert order[0] == seeded["FAIL"] and order[1] == seeded["P1"]
    assert page.counts == {"review": 6, "accepted": 0, "discarded": 0, "skipped": 0, "skipped_failed": 0} and page.total == 6
    p1 = next(c for c in page.items if c.id == seeded["P1"])
    assert p1.ai_verdict == "genuine" and p1.offering_name == "AI corporate workshops"
    assert p1.keywords == ["ai workshop"] and p1.source_ref == "urn:li:activity:7300000000000000001"
    assert (p1.cost_usd_micros, p1.tokens_total) == (2000, 1200)


def test_accept_then_discard_instead_appends_two_decisions_and_history(seeded: dict[str, int]) -> None:
    p1 = seeded["P1"]
    with session_scope() as db:
        [card] = inbox.decide(db, [p1], "accept")
        assert (card.review_status, card.stage, card.is_override) == ("accepted", "prospect_new", False)
    with session_scope() as db:
        assert inbox.list_enquiries(db, "accepted", None).counts == {"review": 5, "accepted": 1, "discarded": 0, "skipped": 0, "skipped_failed": 0}
        [card] = inbox.decide(db, [p1], "discard", "Not a requirement")
        assert (card.review_status, card.stage, card.discard_reason) == ("discarded", None, "Not a requirement")
        assert card.is_override is True  # AI said genuine
    with session_scope() as db:
        decisions = db.scalars(select(TriageDecision).where(TriageDecision.enquiry_id == p1).order_by(TriageDecision.id)).all()
        assert [(d.decision, d.is_override) for d in decisions] == [("accept", False), ("discard", True)]
        fields = db.scalars(select(StatusHistory.field).where(StatusHistory.entity_id == p1,
                                                             StatusHistory.entity_type == "enquiry")).all()
        assert fields.count("review_status") == 2 and "stage" in fields


def test_bulk_discard_uses_one_reason_and_ai_reason_fallback(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        cards = inbox.decide(db, [seeded["P2"], seeded["P3"]], "discard", "Looking for individual trainers")
        assert {c.discard_reason for c in cards} == {"Looking for individual trainers"}
        assert all(not c.is_override for c in cards)
    with session_scope() as db:
        [card] = inbox.decide(db, [seeded["P9"]], "discard")
        assert card.discard_reason == "Company seeking a training partner for an on-site GenAI workshop"
        page = inbox.list_enquiries(db, "discarded", "FREELANCE")
        assert {c.id for c in page.items} == {seeded["P2"], seeded["P3"], seeded["P9"]}
        assert inbox.list_enquiries(db, "discarded", "zzzz").items == []
        assert inbox.list_enquiries(db, "discarded", "100%_").total == 0  # LIKE wildcards are escaped


def test_accepting_an_ai_discard_is_an_override(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        [card] = inbox.decide(db, [seeded["P9"]], "accept")
    assert card.is_override is True


def test_failed_triage_can_be_decided_but_pending_cannot(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        [card] = inbox.decide(db, [seeded["FAIL"]], "discard", "Other")
        assert card.review_status == "discarded" and card.is_override is False
    with session_scope() as db, pytest.raises(DomainError) as info:
        inbox.decide(db, [seeded["PEND"]], "accept")
    assert info.value.code == "decision_invalid"


def test_decide_rejects_empty_unknown_decision_and_missing_ids(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        for args in (([], "accept"), ([seeded["P1"]], "maybe")):
            with pytest.raises(DomainError) as info:
                inbox.decide(db, *args)  # type: ignore[arg-type]
            assert info.value.code == "decision_invalid"
        with pytest.raises(DomainError) as missing:
            inbox.decide(db, [seeded["P1"], 99999], "accept")
        assert missing.value.code == "not_found"
    with session_scope() as db:  # all-or-nothing: P1 untouched
        assert inbox.get_card(db, seeded["P1"]).review_status == "pending_review"


def test_priority_change_keeps_ai_value_and_is_recorded_on_the_decision(seeded: dict[str, int]) -> None:
    p1 = seeded["P1"]
    with session_scope() as db:
        inbox.decide(db, [p1], "accept")
    with session_scope() as db:
        card = inbox.update_fields(db, p1, priority="medium")
        assert (card.priority, card.ai_priority, card.offering_id) == ("medium", "high", 1)
    reset_engine()  # "reload": a fresh engine and session
    with session_scope() as db:
        e = db.get(Enquiry, p1)
        assert e is not None and (e.priority, e.ai_priority) == ("medium", "high")
        latest = db.scalars(select(TriageDecision).where(TriageDecision.enquiry_id == p1)).one()
        assert latest.field_overrides == {"priority": ["high", "medium"]}


def test_update_fields_sets_no_match_and_rejects_bad_values(seeded: dict[str, int]) -> None:
    p1 = seeded["P1"]
    with session_scope() as db:
        assert inbox.update_fields(db, p1, offering_id=None).offering_id is None
        assert inbox.update_fields(db, p1, offering_id=3).offering_name == "AI consulting & build services"
        assert inbox.update_fields(db, p1, priority="low").offering_id == 3  # omitted offering unchanged
        with pytest.raises(DomainError) as bad:
            inbox.update_fields(db, p1, priority="urgent")
        assert bad.value.code == "field_invalid"
        with pytest.raises(DomainError) as missing:
            inbox.update_fields(db, p1, offering_id=999)
        assert missing.value.code == "not_found"
    with session_scope() as db:
        inbox.decide(db, [p1], "accept")
    with session_scope() as db:
        d = db.scalars(select(TriageDecision).where(TriageDecision.enquiry_id == p1)).one()
        assert d.field_overrides == {"priority": ["high", "low"], "offering_id": [1, 3]}


def test_empty_db_inbox_has_zero_counts_and_clamps_page(db_url: str) -> None:
    with session_scope() as db:
        page = inbox.list_enquiries(db, "review", "  ", page=0)
    assert (page.items, page.total, page.page, page.q) == ([], 0, 1, None)
    assert page.counts == {"review": 0, "accepted": 0, "discarded": 0, "skipped": 0, "skipped_failed": 0}


def test_decisions_survive_an_engine_restart(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        inbox.decide(db, [seeded["P1"]], "accept")
        inbox.decide(db, [seeded["P2"]], "discard", "Training agency / marketplace")
    reset_engine()
    with session_scope() as db:
        assert [c.id for c in inbox.list_enquiries(db, "accepted", None).items] == [seeded["P1"]]
        assert [c.id for c in inbox.list_enquiries(db, "discarded", None).items] == [seeded["P2"]]


def test_pagination_returns_page_two(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        page = inbox.list_enquiries(db, "review", None, page=2, page_size=4)
    assert (len(page.items), page.total, page.has_prev, page.has_next) == (2, 6, True, False)


def test_retry_triage_refuses_an_already_triaged_enquiry_without_calling_the_llm(seeded: dict[str, int]) -> None:
    with session_scope() as db:
        calls_before = len(db.scalars(select(LlmCall)).all())
        with pytest.raises(DomainError) as info:
            inbox.retry_triage(db, seeded["P1"])
    assert info.value.code == "decision_invalid" and info.value.http_status == 400
    assert "only for failed triage" in info.value.message
    with session_scope() as db:
        e = db.get(Enquiry, seeded["P1"])
        assert e is not None and e.triage_status == "done" and e.ai_verdict == "genuine"
        assert len(db.scalars(select(LlmCall)).all()) == calls_before


# ---------------------------------------------------------------------- Phase 1.2: card link kind
def test_card_link_kind_is_derived_from_the_source_url_shape(db_url: str) -> None:
    from sales0.services import dedupe
    from tests.integration.services.helpers import SEARCH_URL, sdui_post

    run_id = make_run()
    with session_scope() as db:
        _, post_id = dedupe.upsert_post(db, sdui_post("K1", "A" * 150, urn="urn:li:activity:7300000000000000005"),
                                        run_id, None, "ai workshop")
        _, profile_id = dedupe.upsert_post(db, sdui_post("K2", "B" * 150), run_id, None, "ai workshop")
        _, search_id = dedupe.upsert_post(db, sdui_post("K3", "C" * 150, slug=""), run_id, None, "ai workshop")
        _, other_id = dedupe.upsert_post(db, sdui_post("K4", "D" * 150), run_id, None, "ai workshop")
        _, none_id = dedupe.upsert_post(db, sdui_post("K5", "E" * 150), run_id, None, "ai workshop")
        db.get(Enquiry, other_id).source_url = "https://example.invalid/somewhere"  # type: ignore[union-attr]
        db.get(Enquiry, none_id).source_url = None  # type: ignore[union-attr]
    with session_scope() as db:
        kinds = {eid: inbox.get_card(db, eid).link_kind
                 for eid in (post_id, profile_id, search_id, other_id, none_id)}
        refs = {eid: inbox.get_card(db, eid).source_ref for eid in (post_id, profile_id)}
        assert inbox.get_card(db, search_id).source_url == SEARCH_URL
    assert kinds == {post_id: "post", profile_id: "profile", search_id: "search", other_id: "other", none_id: None}
    assert refs == {post_id: "urn:li:activity:7300000000000000005", profile_id: "ck:K2"}

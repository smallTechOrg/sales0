"""E3: the category is stored from the triage call (no extra LLM call), editable, filterable; channel partner ticks
Partner on accept; competitor is filed into the registry with the enquiry kept as evidence."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Competitor, CompetitorMention, Enquiry, EntityCharacteristic, LlmCall, Prospect, StatusHistory
from sales0.errors import DomainError
from sales0.services import categories, inbox
from sales0.services.crm import characteristics, prospects
from sales0.services.triage import triage_enquiry
from tests.integration.crm import helpers as h
from tests.integration.services.helpers import make_enquiry as make_post
from tests.support.fake_llm import FakeLLMClient, triage_answer

TEXT = "BrightMinds Academy delivers corporate GenAI training. Book a demo."


def _enq(db, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("status", "pending_review")
    kw.setdefault("ai_verdict", "genuine")
    return h.enquiry(db, **kw)


def test_triage_stores_the_suggestion_with_one_llm_call(db_url: str) -> None:
    eid = make_post(TEXT)
    fake = FakeLLMClient([triage_answer(category="competitor", verdict="discard", priority="low")])
    with session_scope() as db:
        triage_enquiry(db, eid, fake)
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert (e.ai_category, e.category) == ("competitor", "competitor")
        assert db.scalar(select(func.count(LlmCall.id)).where(LlmCall.enquiry_id == eid)) == 1  # no extra call per post


def test_set_category_records_history_and_validates(db, installed) -> None:
    e = _enq(db)
    categories.set_category(db, e.id, "channel_partner")
    assert db.get(Enquiry, e.id).category == "channel_partner"
    n = lambda: db.scalar(select(func.count(StatusHistory.id)).where(StatusHistory.entity_id == e.id, StatusHistory.field == "category"))  # noqa: E731
    assert n() == 1
    categories.set_category(db, e.id, "channel_partner")  # no-op
    assert n() == 1
    with pytest.raises(DomainError) as exc:
        categories.set_category(db, e.id, "vendor")
    assert exc.value.code == "category_invalid"


def test_inbox_filter_by_category(db, installed) -> None:
    a = _enq(db)
    b = _enq(db, category="channel_partner")
    assert {c.id for c in inbox.list_enquiries(db, "review", None).items} == {a.id, b.id}
    got = inbox.list_enquiries(db, "review", None, category="channel_partner")
    assert [c.id for c in got.items] == [b.id] and got.items[0].category == "channel_partner"
    assert inbox.list_enquiries(db, "review", None, category="bogus").total == 2  # unknown filter is ignored


def test_accepting_a_channel_partner_ticks_the_partner_characteristic(db, installed) -> None:
    e = _enq(db, category="channel_partner", company="Learnbridge")
    inbox.decide(db, [e.id], "accept")
    pid = db.get(Enquiry, e.id).prospect_id
    assert [c.name for c in characteristics.for_prospects(db, [pid])[pid]] == ["Partner"]
    # changing the category on an already-accepted enquiry ticks it too
    other = _enq(db, company="Other Co")
    inbox.decide(db, [other.id], "accept")
    opid = db.get(Enquiry, other.id).prospect_id
    assert not characteristics.for_prospects(db, [opid]).get(opid)
    categories.set_category(db, other.id, "channel_partner")
    assert [c.name for c in characteristics.for_prospects(db, [opid])[opid]] == ["Partner"]
    assert db.scalar(select(func.count(EntityCharacteristic.id))) == 2


def test_file_as_competitor_keeps_the_enquiry_as_evidence_and_skips_the_pipeline(db, installed) -> None:
    e = _enq(db, ai_verdict="discard", category="competitor", company="BrightMinds Academy")
    row = categories.file_as_competitor(db, e.id, website="brightminds.example")
    assert row.name == "BrightMinds Academy"
    db.refresh(e)
    assert e.review_status == "discarded" and "Filed as competitor" in (e.discard_reason or "") and e.prospect_id is None
    assert db.scalar(select(func.count(Prospect.id))) == 0
    mention = db.scalar(select(CompetitorMention).where(CompetitorMention.entity_type == "enquiry", CompetitorMention.entity_id == e.id))
    assert mention is not None and mention.competitor_id == row.id and mention.source == "user"
    card = inbox.get_card(db, e.id)
    assert card.competitor_name == "BrightMinds Academy" and card.category == "competitor"
    # a second post of the same company reuses the registry row
    e2 = _enq(db, ai_verdict="discard", company="BrightMinds Academy")
    again = categories.file_as_competitor(db, e2.id)
    assert again.id == row.id and db.scalar(select(func.count(Competitor.id)).where(Competitor.name == 'BrightMinds Academy')) == 1


def test_file_as_competitor_needs_a_name_and_closes_an_untouched_prospect(db, installed) -> None:
    anon = _enq(db, ai_verdict="discard", author_name=None)
    with pytest.raises(DomainError) as exc:
        categories.file_as_competitor(db, anon.id)
    assert exc.value.code == "competitor_invalid"
    accepted = _enq(db, company="Rival Co")
    inbox.decide(db, [accepted.id], "accept")
    pid = db.get(Enquiry, accepted.id).prospect_id
    categories.file_as_competitor(db, accepted.id)
    p = prospects.get_prospect(db, pid)
    assert p.status == "closed" and p.closed_reason == "competitor"

"""Audit @7ae3d66 #6: reply classification has its own llm feature, honours the spend cap (defers, never drops)
and does not inflate the triage cost-per-enquiry."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.models import LlmCall
from sales0.services import replies
from sales0.services.costs_report import FEATURES, build_report
from tests.integration.replies.conftest import Team, all_replies, reply, sent_message
from tests.integration.crm import helpers as h
from tests.support.fake_llm import FakeLLMClient

HUMAN = {"kind": "human", "intent": "interested", "reason": "wants a call"}


def test_reply_classification_is_booked_as_its_own_feature(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="Interested, send details")
    llm = FakeLLMClient([HUMAN])
    replies.poll_all(llm=llm)
    db.expire_all()
    assert llm.requests[0].feature == "reply"
    assert [r.feature for r in db.scalars(select(LlmCall))] == ["reply"]


def test_over_the_spend_cap_the_reply_is_deferred_not_dropped_then_retried(
    db: Session, team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "1")
    reset_settings()
    db.add(LlmCall(feature="triage", model="m", status="ok", cost_usd_micros=2_000_000))
    db.commit()
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="Interested, send details")
    llm = FakeLLMClient([HUMAN])
    (r1,) = replies.poll_all(llm=llm)
    assert llm.calls == 0, "no LLM call over the cap"
    assert r1.skipped_deferred == 1 and r1.new_replies == 0 and r1.status == "ok" and "retried" in (r1.error or "")
    assert all_replies(db) == []
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0")  # cap lifted / new month
    reset_settings()
    (r2,) = replies.poll_all(llm=llm, force=True)
    assert llm.calls == 1 and r2.new_replies == 1 and len(all_replies(db)) == 1


def test_reply_calls_do_not_inflate_cost_per_enquiry(db: Session) -> None:
    e = h.enquiry(db)
    db.add(LlmCall(feature="triage", model="m", status="ok", enquiry_id=e.id, cost_usd_micros=1000))
    db.add(LlmCall(feature="reply", model="m", status="ok", cost_usd_micros=5000))
    db.commit()
    rep = build_report(db)
    assert rep.triaged_enquiries == 1 and rep.cost_per_enquiry_micros == 1000
    assert {b.key for b in rep.by_feature} == {"triage", "reply"} and "reply" in FEATURES

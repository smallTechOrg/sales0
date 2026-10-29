"""E2: AI-drafted follow-up EMAILS. Gmail and the LLM are fakes. The point of the whole feature is what does NOT happen:
nothing is approved or sent without a human; replies / unsubscribes / closed prospects stop the drafts."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import FollowUp, LlmCall, OutreachEvent, OutreachMessage, Prospect
from sales0.errors import DomainError
from sales0.services import events, followup_drafts, suppressions
from sales0.services.crm import flags, followups, prospects
from sales0.services.crm.actor import SYSTEM
from sales0.services.outreach import messages
from sales0.services.research import docs
from tests.integration.crm import helpers as h
from tests.integration.crm import conftest as _crm
from tests.integration.outreach import conftest as _outreach
from tests.integration.outreach.conftest import Team, make_message
from tests.support.fake_llm import FakeLLMClient

db = _outreach.db
team = _outreach.team
installed = _crm.installed

REPLY = {
    "subject": "ignored - the program keeps the thread subject",
    "body": "Hello Priya,\n\nA quick note on my earlier email about the GenAI workshop for Acme Training. We can share a short outline if useful.\n\nBest regards,\nAnn Rao",
}


def _llm() -> FakeLLMClient:
    return FakeLLMClient(responder=lambda req: dict(REPLY))


def _first_contact(db: Session, team: Team, *, name: str = "Acme Training", email: str = "priya@acme.example", member=None):  # type: ignore[no-untyped-def]
    """An accepted enquiry -> prospect, first email approved + SENT with the fake Gmail (reminders at +3/+7/+14)."""
    member = member or team.ann
    e = h.enquiry(db, profile_row=h.profile(db, slug=f"in/fd{h._ids.__next__()}"), company=name, email=email, contact_person="Priya Nair")
    v = prospects.ensure_for_enquiry(db, e.id)
    msg = make_message(db, member, to=email, subject="AI workshops for Acme Training",
                       body="Hello Priya,\n\nWe run GenAI workshops for corporate teams.\n\nBest regards,\nAnn Rao")
    msg.enquiry_id, msg.prospect_id = e.id, v.id
    db.commit()
    messages.approve(db, msg.id, member)
    sent = messages.send(db, msg.id, member)
    assert sent.state == "sent"
    return v, e, sent


def _later(days: int = 4):  # type: ignore[no-untyped-def]
    return h.T0 + timedelta(days=days)  # only used as a clock for the follow-up logic


def _now_plus(db: Session, msg: OutreachMessage, days: int):  # type: ignore[no-untyped-def]
    return msg.sent_at + timedelta(days=days)


def _fu1(db: Session, pid: int) -> FollowUp:
    return db.scalar(select(FollowUp).where(FollowUp.prospect_id == pid, FollowUp.kind == "follow_up_1"))


# ------------------------------------------------------------------------------------------------ drafting
def test_due_email_follow_up_becomes_a_draft_in_the_thread_and_nothing_is_sent(db: Session, team: Team, installed) -> None:
    v, e, first = _first_contact(db, team)
    docs.add_version(db, v.id, "summary", origin="human", actor=SYSTEM, body="Acme is planning a GenAI rollout in Q1.")
    docs.add_version(db, v.id, "research", origin="human", actor=SYSTEM, data={"what_they_do": "Corporate training for logistics firms"})
    db.commit()
    llm = _llm()
    sent_before = len(team.gmail[team.ann.id].sent)
    res = followup_drafts.draft_due(db, llm, team.ann, now=_now_plus(db, first, 4))
    assert (res.drafted, res.skipped, res.failed, res.remaining) == (1, 0, 0, 0)
    draft = db.get(OutreachMessage, res.outcomes[0].message_id)
    assert draft.state == "draft" and draft.member_id == team.ann.id and draft.deck_id is None
    assert draft.follow_up_id == _fu1(db, v.id).id and draft.follow_up_of_id == first.id and draft.prospect_id == v.id
    assert draft.subject == "Re: AI workshops for Acme Training" and draft.to_addrs == "priya@acme.example"
    assert draft.content_sha256 is None and draft.approved_at is None
    # the model saw the follow-up variant, the earlier email, the research and the summary
    req = llm.requests[0]
    user = req.messages[0]["content"]
    assert req.feature == "drafting" and req.prompt_version == "draft-followup-v1" and "FOLLOW-UP" in req.system
    assert "We run GenAI workshops" in user and "Follow-up 1" in user and "GenAI rollout in Q1" in user
    assert "Corporate training for logistics firms" in user
    row = db.scalar(select(LlmCall).where(LlmCall.feature == "drafting").order_by(LlmCall.id.desc()))
    assert row.prompt_version == "draft-followup-v1" and row.status == "ok"
    # NOTHING was approved or sent: the standing rule
    assert len(team.gmail[team.ann.id].sent) == sent_before
    assert db.scalar(select(func.count(OutreachEvent.id)).where(OutreachEvent.message_id == draft.id, OutreachEvent.kind.in_(("approved", "sending", "sent")))) == 0
    assert not _fu1(db, v.id).done_at


def test_approve_and_send_marks_exactly_that_reminder_done_by_email_and_stays_in_the_thread(db: Session, team: Team, installed) -> None:
    v, e, first = _first_contact(db, team)
    res = followup_drafts.draft_due(db, _llm(), team.ann, now=_now_plus(db, first, 4))
    draft_id = res.outcomes[0].message_id
    gm = team.gmail[team.ann.id]
    messages.approve(db, draft_id, team.ann)
    sent = messages.send(db, draft_id, team.ann)
    assert sent.state == "sent" and sent.gmail_thread_id == first.gmail_thread_id  # same Gmail conversation
    raw = gm.sent[-1]["raw"].decode()
    assert "In-Reply-To: <msg-1@fake.example>" in raw and "References: <msg-1@fake.example>" in raw
    assert any(c[0] == "get_thread" for c in gm.calls)
    f = db.get(FollowUp, sent.follow_up_id)
    assert f.done_at is not None and f.channel == "email"
    p = prospects.get_prospect(db, v.id)
    assert p.stage == "follow_up_1" and p.next_follow_up_on == db.scalar(select(FollowUp.due_on).where(FollowUp.prospect_id == v.id, FollowUp.kind == "follow_up_2"))
    assert [t for t in p.timeline if t.kind == "follow_up_done" and t.data.get("channel") == "email"]
    # the other two reminders are untouched
    assert sorted(x.kind for x in db.scalars(select(FollowUp).where(FollowUp.prospect_id == v.id, FollowUp.done_at.is_(None)))) == ["final_follow_up", "follow_up_2"]


def test_only_the_due_email_reminders_of_the_right_state_are_drafted_with_reasons(db: Session, team: Team, installed) -> None:
    now_days = 20
    # 1 replied, 2 unsubscribed, 3 closed, 4 do-not-follow-up, 5 planned WhatsApp, 6 fine
    cases = {}
    for key in ("replied", "unsub", "closed", "nofu", "whatsapp", "fine"):
        v, e, first = _first_contact(db, team, name=f"{key.title()} Co", email=f"{key}@x.example")
        cases[key] = (v, first)
    prospects.record_response(db, cases["replied"][0].id, now=h.T0)
    suppressions.add(db, "unsub@x.example", "unsubscribe", note="asked to stop")
    prospects.close(db, cases["closed"][0].id, "lost")
    flags.set_flag(db, "prospect", cases["nofu"][0].id, "no_follow_up", source="user")
    db.commit()
    for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == cases["whatsapp"][0].id)):
        followups.set_planned_channel(db, f.id, "whatsapp")
    db.commit()
    now = _now_plus(db, cases["fine"][1], now_days)
    res = followup_drafts.draft_due(db, _llm(), team.ann, limit=50, now=now)
    drafted = [o for o in res.outcomes if o.status == "drafted"]
    assert [(o.prospect_name, o.kind_label) for o in drafted] == [("Fine Co", "Follow-up 1")]  # ONE at a time per prospect
    assert [o.reason for o in res.outcomes if o.prospect_name == "Fine Co" and o.status == "skipped"] == ["Follow-up 1 comes first"] * 2
    unsub = [o for o in res.outcomes if o.prospect_name.startswith("Unsub")]
    assert unsub and unsub[0].status == "skipped" and "suppression" in unsub[0].reason
    what = [o for o in res.outcomes if o.prospect_name.startswith("Whatsapp")]
    assert what and what[0].status == "skipped" and "WhatsApp" in what[0].reason
    assert not [o for o in res.outcomes if o.prospect_name.startswith(("Replied", "Closed", "Nofu"))]  # their reminders were cancelled
    # a drafted one is never drafted twice
    again = followup_drafts.draft_due(db, _llm(), team.ann, limit=50, now=now)
    assert again.drafted == 0 and any("already exists" in (o.reason or "") for o in again.outcomes)


def test_eligibility_reasons_for_replied_converted_and_foreign_threads(db: Session, team: Team, installed) -> None:
    v, e, first = _first_contact(db, team, name="Reply Co", email="r@reply.example")
    f = _fu1(db, v.id)
    # a human reply that the CRM has not cancelled yet (e.g. recorded on another path): the drafter still stops
    h.reply(db, first, kind="human", intent="question")
    db.commit()
    assert "replied" in followup_drafts.check(db, f, team.ann, require_due=False).reason
    v2, e2, first2 = _first_contact(db, team, name="Bob Thread", email="b@bob.example", member=team.bob)
    f2 = _fu1(db, v2.id)
    why = followup_drafts.check(db, f2, team.ann, require_due=False)
    assert not why.ok and "Bob Das" in why.reason
    assert followup_drafts.check(db, f2, team.bob, require_due=False).ok
    with pytest.raises(DomainError) as exc:
        followup_drafts.draft_follow_up(db, _llm(), f2.id, team.ann)
    assert exc.value.code == "followup_not_draftable" and "Bob Das" in exc.value.message


def test_cap_per_click_and_click_again(db: Session, team: Team, installed) -> None:
    firsts = [_first_contact(db, team, name=f"Batch{i} Co", email=f"b{i}@batch.example") for i in range(3)]
    now = _now_plus(db, firsts[0][2], 5)
    llm = _llm()
    res = followup_drafts.draft_due(db, llm, team.ann, limit=2, now=now)
    assert (res.drafted, res.remaining) == (2, 1) and len(llm.requests) == 2
    assert "click again" in res.summary()
    res2 = followup_drafts.draft_due(db, llm, team.ann, limit=2, now=now)
    assert (res2.drafted, res2.remaining) == (1, 0)
    assert get_settings().followup_draft_batch == 10


def test_spend_cap_stops_the_batch_without_calling_the_ai(db: Session, team: Team, installed, monkeypatch: pytest.MonkeyPatch) -> None:
    v, e, first = _first_contact(db, team)
    monkeypatch.setenv("SALES0_MONTHLY_SPEND_CAP_USD", "0.0001")
    from sales0.config import reset_settings

    reset_settings()
    db.add(LlmCall(feature="drafting", model="m", prompt_version="x", status="ok", cost_usd_micros=5_000_000, created_at=_now_plus(db, first, 4)))
    db.commit()
    llm = _llm()
    res = followup_drafts.draft_due(db, llm, team.ann, now=_now_plus(db, first, 4))
    assert res.drafted == 0 and res.stopped and "cap" in res.stopped.lower() and not llm.requests


# ------------------------------------------------------------------------------------------------ stop rules
def test_a_reply_cancels_the_reminders_and_rejects_the_pending_drafts(db: Session, team: Team, installed) -> None:
    v, e, first = _first_contact(db, team)
    res = followup_drafts.draft_due(db, _llm(), team.ann, now=_now_plus(db, first, 4))
    draft = db.get(OutreachMessage, res.outcomes[0].message_id)
    r = h.reply(db, first, kind="human", intent="interested")
    events.publish(db, events.ReplyReceived(outreach_message_id=first.id, kind="human", intent="interested", reply_id=r.id, member_id=team.ann.id,
                                            from_addr=r.from_addr))
    db.commit()
    db.refresh(draft)
    assert draft.state == "rejected" and "Follow-up stopped" in (draft.error or "")
    with pytest.raises(DomainError):
        messages.approve(db, draft.id, team.ann)  # a rejected draft cannot be revived


def test_a_draft_whose_reminder_was_cancelled_or_done_by_hand_cannot_be_sent(db: Session, team: Team, installed) -> None:
    v, e, first = _first_contact(db, team)
    res = followup_drafts.draft_due(db, _llm(), team.ann, now=_now_plus(db, first, 4))
    draft = db.get(OutreachMessage, res.outcomes[0].message_id)
    messages.approve(db, draft.id, team.ann)
    f = db.get(FollowUp, draft.follow_up_id)
    f.cancelled_at, f.cancel_reason = h.T0, "response_received"  # stopped behind the approved draft's back
    db.commit()
    with pytest.raises(DomainError) as exc:
        messages.send(db, draft.id, team.ann)
    assert exc.value.code == "follow_up_stopped"
    assert not team.gmail[team.ann.id].sent[1:]  # only the first email ever went out
    db.refresh(draft)
    assert draft.state == "approved"


def test_marking_it_done_by_hand_or_changing_the_channel_rejects_the_draft(db: Session, team: Team, installed) -> None:
    v, e, first = _first_contact(db, team)
    res = followup_drafts.draft_due(db, _llm(), team.ann, now=_now_plus(db, first, 4))
    draft = db.get(OutreachMessage, res.outcomes[0].message_id)
    followups.set_planned_channel(db, draft.follow_up_id, "call")
    db.commit()
    db.refresh(draft)
    assert draft.state == "rejected" and "Call" in draft.error
    # done by hand on another reminder
    v2, e2, first2 = _first_contact(db, team, name="Hand Co", email="h@hand.example")
    r2 = followup_drafts.draft_due(db, _llm(), team.ann, now=_now_plus(db, first2, 4))
    d2 = db.get(OutreachMessage, [o for o in r2.outcomes if o.status == "drafted"][0].message_id)
    followups.complete(db, d2.follow_up_id, channel="whatsapp", note="spoke")
    db.commit()
    db.refresh(d2)
    assert d2.state == "rejected"


# ------------------------------------------------------------------------------------------------ planned channel
def test_log_contact_stores_a_planned_channel_per_follow_up_default_email(db: Session, installed) -> None:
    p = prospects.create_manual(db, name="Planned Co", email="p@planned.example")
    prospects.log_contact(db, p.id, channel="whatsapp", channels={"follow_up_1": "whatsapp", "final_follow_up": "call"})
    got = {f.kind: f.planned_channel for f in db.scalars(select(FollowUp).where(FollowUp.prospect_id == p.id))}
    assert got == {"follow_up_1": "whatsapp", "follow_up_2": "email", "final_follow_up": "call"}
    with pytest.raises(DomainError):
        followups.set_planned_channel(db, db.scalar(select(FollowUp.id).where(FollowUp.prospect_id == p.id)), "carrier-pigeon")


def test_first_contact_by_whatsapp_then_email_follow_up_without_a_thread(db: Session, team: Team, installed) -> None:
    p = prospects.create_manual(db, name="Wa Co", company="Wa Co", email="wa@wa.example")
    prospects.log_contact(db, p.id, channel="whatsapp", now=h.T0)
    f = _fu1(db, p.id)
    res = followup_drafts.draft_due(db, _llm(), team.ann, now=h.T0 + timedelta(days=4))
    assert res.drafted == 1
    draft = db.get(OutreachMessage, res.outcomes[0].message_id)
    assert draft.follow_up_of_id is None and draft.to_addrs == "wa@wa.example" and not draft.subject.startswith("Re:")
    messages.approve(db, draft.id, team.ann)
    sent = messages.send(db, draft.id, team.ann)
    assert sent.state == "sent"
    db.expire_all()
    assert db.get(FollowUp, f.id).channel == "email" and db.get(Prospect, p.id).stage == "follow_up_1"


# ------------------------------------------------------------------------------------------------ scheduler (opt-in)
def test_the_scheduled_job_is_off_by_default_and_only_drafts(db: Session, team: Team, installed) -> None:
    from sales0.jobs.scheduler import FollowUpDraftsJob, Scheduler

    assert FollowUpDraftsJob.default_enabled is False
    v, e, first = _first_contact(db, team)
    s = Scheduler()
    assert {j.name: j.enabled for j in s.snapshot()}["followup_drafts"] is False
    assert s.tick() == [] or "followup_drafts" not in s.tick()
    # run it once by hand (manual run = the same code the daily tick calls), with the clock past the due date
    import sales0.services.followup_drafts as svc

    res = svc.draft_due(db, _llm(), None, now=_now_plus(db, first, 4))  # member None = the scheduler path
    assert res.drafted == 1 and db.get(OutreachMessage, res.outcomes[0].message_id).member_id == team.ann.id
    assert len(team.gmail[team.ann.id].sent) == 1  # still only the first email

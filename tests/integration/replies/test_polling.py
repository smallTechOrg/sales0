"""Polling: idempotency, own-address exclusion, per-member separation, classification routing, read-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.config import reset_settings
from sales0.db.models import LlmCall, ReplyPollState
from sales0.integrations.gmail.threads import ThreadMessage
from sales0.llm import LLMAuthError, LLMBadResponse, LLMTransientError
from sales0.services import replies
from tests.integration.replies.conftest import Team, all_replies, fresh, reply, sent_message
from tests.support.fake_llm import FakeLLMClient

HUMAN = {"kind": "human", "intent": "interested", "reason": "wants a call"}


def llm_rows(db: Session) -> int:
    db.expire_all()
    return int(db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.feature == "reply")) or 0)


def state(db: Session, member_id: int) -> ReplyPollState:
    db.expire_all()
    return db.scalars(select(ReplyPollState).where(ReplyPollState.member_id == member_id)).one()


# ------------------------------------------------------------------ idempotency
def test_a_second_poll_stores_nothing_new_and_makes_zero_llm_calls(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="Yes, can we talk Thursday?")
    llm = FakeLLMClient([HUMAN])
    (r1,) = replies.poll_all(llm=llm)
    assert r1.new_replies == 1 and r1.status == "ok" and llm.calls == 1
    (r2,) = replies.poll_all(llm=llm, force=True)
    assert r2.new_replies == 0 and r2.threads_checked >= 0 and llm.calls == 1
    rows = all_replies(db)
    assert len(rows) == 1 and rows[0].kind == "human" and rows[0].intent == "interested" and rows[0].classified_by == "llm"
    assert rows[0].from_addr == "p@acme.example" and rows[0].message_id == msg.id and rows[0].member_id == team.ann.id
    assert rows[0].snippet == "Yes, can we talk Thursday?" and rows[0].handled is False
    assert llm_rows(db) == 1


def test_a_second_message_in_the_thread_is_picked_up_later_and_old_ones_are_not_reclassified(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="first")
    llm = FakeLLMClient([HUMAN, {"kind": "human", "intent": "question", "reason": "asks"}])
    replies.poll_all(llm=llm)
    reply(team, team.ann, msg, snippet="second")
    replies.poll_all(llm=llm, force=True)
    assert [r.snippet for r in all_replies(db)] == ["first", "second"] and llm.calls == 2


def test_reply_stores_only_a_snippet_and_an_event_row(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="x" * 900)
    replies.poll_all(llm=FakeLLMClient([HUMAN]))
    (row,) = all_replies(db)
    assert len(row.snippet) <= 500
    from sales0.services.outreach import messages

    assert "reply_received" in [e.kind for e in messages.list_events(db, msg.id)]


# ------------------------------------------------------------------ own addresses
def test_own_addresses_are_never_replies(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_REPLY_OWN_ALIASES", "Alias@Team.Example; other-alias@team.example")
    reset_settings()
    msg = sent_message(db, team, team.ann)
    gm = team.gmail[team.ann.id]
    reply(team, team.ann, msg, team.ann.email, snippet="my own follow-up note")
    reply(team, team.ann, msg, "alias@team.example")
    reply(team, team.ann, msg, "other-alias@team.example")
    reply(team, team.ann, msg, team.bob.email, snippet="a teammate answering in the thread")
    reply(team, team.ann, msg, team.admin.email)
    gm.add_reply(msg.gmail_thread_id, "p@acme.example", labels=("DRAFT",), snippet="a draft of mine")
    llm = FakeLLMClient([])
    (r,) = replies.poll_all(llm=llm)
    assert r.new_replies == 0 and llm.calls == 0 and all_replies(db) == []
    assert fresh(db, msg).reply_status == "none"


def test_the_sent_message_itself_and_sent_labelled_mail_never_count(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    team.gmail[team.ann.id].add_reply(msg.gmail_thread_id, "p@acme.example", labels=("SENT",), snippet="sent from elsewhere")
    (r,) = replies.poll_all(llm=FakeLLMClient([]))
    assert r.threads_checked == 1 and r.new_replies == 0


def test_messages_older_than_the_send_are_ignored(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann, sent_at=datetime.now(UTC) - timedelta(hours=1))
    reply(team, team.ann, msg, date=datetime.now(UTC) - timedelta(days=3), snippet="from an older exchange")
    (r,) = replies.poll_all(llm=FakeLLMClient([]))
    assert r.new_replies == 0


# ------------------------------------------------------------------ per-member separation
def test_a_reply_is_counted_only_for_the_member_whose_mailbox_it_is(db: Session, team: Team) -> None:
    a = sent_message(db, team, team.ann, to="p@acme.example")
    b = sent_message(db, team, team.bob, to="q@beta.example")
    reply(team, team.bob, b, "q@beta.example", snippet="Bob's prospect answers")
    llm = FakeLLMClient([HUMAN])
    results = {r.member_id: r for r in replies.poll_all(llm=llm)}
    assert results[team.ann.id].new_replies == 0 and results[team.bob.id].new_replies == 1
    (row,) = all_replies(db)
    assert row.member_id == team.bob.id and row.message_id == b.id
    assert fresh(db, a).reply_status == "none" and fresh(db, b).reply_status == "replied"


def test_polling_one_member_never_touches_the_other_members_client(db: Session, team: Team) -> None:
    sent_message(db, team, team.ann)
    sent_message(db, team, team.bob)
    replies.poll_member(team.ann.id, llm=FakeLLMClient([]))
    assert team.gmail[team.bob.id].calls == []
    assert {c[0] for c in team.gmail[team.ann.id].calls} == {"profile_email", "get_thread"}


def test_a_message_id_seen_in_another_members_thread_is_not_double_counted(db: Session, team: Team) -> None:
    """Same Gmail message id in two mailboxes (e.g. both on the To line): the UNIQUE id keeps one row."""
    a = sent_message(db, team, team.ann)
    b = sent_message(db, team, team.bob, to="p@acme.example")
    m = reply(team, team.ann, a, snippet="hi both")
    team.gmail[team.bob.id].threads[b.gmail_thread_id].append(
        ThreadMessage(id=m.id, thread_id=b.gmail_thread_id, from_addr="p@acme.example", from_name="", to_addrs=(), cc_addrs=(), subject="Re", date=m.date, snippet="hi both", labels=("INBOX",))
    )
    replies.poll_all(llm=FakeLLMClient([HUMAN, HUMAN]))
    assert len(all_replies(db)) == 1


def test_inactive_members_and_members_without_sent_mail_are_not_polled(db: Session, team: Team) -> None:
    sent_message(db, team, team.ann)
    results = replies.poll_all(llm=FakeLLMClient([]))
    assert [r.member_id for r in results] == [team.ann.id]
    team.ann.active = False
    db.commit()
    assert replies.poll_all(llm=FakeLLMClient([])) == []


# ------------------------------------------------------------------ classification routing
def test_header_decided_replies_make_no_llm_call(db: Session, team: Team) -> None:
    ooo = sent_message(db, team, team.ann, to="a@acme.example")
    bounce = sent_message(db, team, team.ann, to="b@acme.example")
    reply(team, team.ann, ooo, "a@acme.example", auto_submitted="auto-replied", subject="Automatic reply: hi", snippet="I am away")
    reply(team, team.ann, bounce, "mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)", snippet="550 no such user")
    llm = FakeLLMClient([])
    (r,) = replies.poll_all(llm=llm)
    assert llm.calls == 0 and llm_rows(db) == 0
    assert r.kinds == {"auto_reply": 1, "bounce": 1}
    assert {(x.kind, x.classified_by, x.intent) for x in all_replies(db)} == {("auto_reply", "headers", None), ("bounce", "headers", None)}


def test_ambiguous_mail_goes_to_the_llm_with_sender_subject_and_snippet(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, subject="Re: Workshop for Acme", snippet="Who are you and how did you get my address?")
    llm = FakeLLMClient([{"kind": "human", "intent": "question", "reason": "who are you"}])
    replies.poll_all(llm=llm)
    assert llm.calls == 1 and llm.requests[0].feature == "reply"
    user = llm.requests[0].messages[0]["content"]
    assert "p@acme.example" in user and "Who are you" in user
    (row,) = all_replies(db)
    assert (row.kind, row.intent, row.classified_by) == ("human", "question", "llm")


def test_llm_transient_failure_defers_then_the_next_cycle_stores_it(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="Interested, send details")
    llm = FakeLLMClient([LLMTransientError("overloaded"), HUMAN])
    (r1,) = replies.poll_all(llm=llm)
    assert r1.skipped_deferred == 1 and r1.new_replies == 0 and r1.status == "ok" and "retry" in (r1.error or "")
    assert all_replies(db) == [] and fresh(db, msg).reply_status == "none"
    assert state(db, team.ann.id).consecutive_failures == 0 and state(db, team.ann.id).last_ok_at is None
    (r2,) = replies.poll_all(llm=llm, force=True)
    assert r2.new_replies == 1 and len(all_replies(db)) == 1 and fresh(db, msg).reply_status == "replied"
    statuses = sorted(db.scalars(select(LlmCall.status).where(LlmCall.feature == "reply")))
    assert statuses == ["error", "ok"]


def test_a_deferred_reply_is_still_found_after_a_successful_cycle_elsewhere(db: Session, team: Team) -> None:
    """last_ok_at is not advanced on a deferred cycle, so the listing window still covers the thread."""
    msg = sent_message(db, team, team.ann, sent_at=datetime.now(UTC) - timedelta(hours=10))
    reply(team, team.ann, msg, snippet="please send details")
    llm = FakeLLMClient([LLMTransientError("x"), HUMAN])
    t0 = datetime.now(UTC)
    replies.poll_member(team.ann.id, llm=llm, now=t0)
    replies.poll_member(team.ann.id, llm=llm, now=t0 + timedelta(minutes=10))
    assert len(all_replies(db)) == 1


def test_a_bad_llm_answer_falls_back_to_human_other(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="???")
    replies.poll_all(llm=FakeLLMClient([{"kind": "martian", "intent": "other", "reason": "x"}]))
    (row,) = all_replies(db)
    assert (row.kind, row.intent, row.classified_by) == ("human", "other", "fallback")
    assert fresh(db, msg).reply_status == "replied"
    replies.poll_all(llm=FakeLLMClient([LLMBadResponse("junk")]), force=True)  # idempotent: nothing more
    assert len(all_replies(db)) == 1


def test_a_fatal_llm_auth_error_is_a_recorded_failure_with_backoff(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="hello")
    t0 = datetime.now(UTC)
    r = replies.poll_member(team.ann.id, llm=FakeLLMClient([LLMAuthError("bad key")]), now=t0)
    assert r.status == "error" and "AI classifier unavailable" in (r.error or "")
    st = state(db, team.ann.id)
    assert st.consecutive_failures == 1 and st.next_attempt_at is not None and st.last_error
    assert all_replies(db) == []
    llm = FakeLLMClient([])
    assert replies.poll_member(team.ann.id, llm=llm, now=t0 + timedelta(seconds=30)).status == "skipped" and llm.calls == 0
    ok = replies.poll_member(team.ann.id, llm=FakeLLMClient([HUMAN]), now=t0 + timedelta(hours=1))
    assert ok.status == "ok" and ok.new_replies == 1 and state(db, team.ann.id).consecutive_failures == 0


# ------------------------------------------------------------------ read-only
def test_polling_only_ever_reads_the_mailbox(db: Session, team: Team) -> None:
    a = sent_message(db, team, team.ann)
    sent_message(db, team, team.bob)
    reply(team, team.ann, a, snippet="yes please")
    reply(team, team.ann, a, "mailer-daemon@x.example", subject="Undelivered Mail Returned to Sender")
    llm = FakeLLMClient([HUMAN])
    replies.poll_all(llm=llm)
    replies.poll_all(llm=llm, force=True)
    replies.poll_all(llm=llm, force=True, now=datetime.now(UTC) + timedelta(days=1))
    seen = {c[0] for gm in team.gmail.values() for c in gm.calls}
    assert seen and seen <= {"profile_email", "get_thread", "list_threads_with_new_messages"}
    assert team.gmail[team.ann.id].sent == [] and not hasattr(team.gmail[team.ann.id], "trashed")
    assert team.gmail[team.ann.id].raw_drafts == {}


def test_unrelated_mailbox_threads_are_not_fetched(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    team.gmail[team.ann.id].threads["someone-elses"] = []
    replies.poll_all(llm=FakeLLMClient([]))
    fetched = [c[1][0] for c in team.gmail[team.ann.id].calls if c[0] == "get_thread"]
    assert fetched == [msg.gmail_thread_id]


def test_deleted_threads_are_skipped_quietly(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    del team.gmail[team.ann.id].threads[msg.gmail_thread_id]
    (r,) = replies.poll_all(llm=FakeLLMClient([]))
    assert r.status == "ok" and r.threads_checked == 0


def test_only_sent_messages_within_the_lookback_are_polled(db: Session, team: Team) -> None:
    old = sent_message(db, team, team.ann, sent_at=datetime.now(UTC) - timedelta(days=90))
    reply(team, team.ann, old, date=datetime.now(UTC), snippet="late reply")
    (r,) = replies.poll_all(llm=FakeLLMClient([]))
    assert r.threads_checked == 0 and all_replies(db) == []

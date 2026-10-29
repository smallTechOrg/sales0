"""ReplyReceived: the default subscriber's effects and custom subscribers."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import OutreachMessage, Suppression
from sales0.services import events, replies, suppressions
from sales0.services.events import ReplyReceived
from sales0.services.outreach import messages
from tests.integration.replies.conftest import Team, all_replies, fresh, make_message, reply, sent_message
from tests.support.fake_llm import FakeLLMClient

HUMAN = {"kind": "human", "intent": "interested", "reason": "wants a call"}


def human(intent: str) -> dict[str, str]:
    return {"kind": "human", "intent": intent, "reason": "test"}


def follow_up(db: Session, team: Team, parent: OutreachMessage, state: str) -> OutreachMessage:
    fu = make_message(db, team.ann, to=parent.to_addrs, subject="Following up", state=state)
    fu.follow_up_of_id = parent.id
    db.commit()
    return fu


def suppressed(db: Session) -> dict[str, str]:
    db.expire_all()
    return {s.address: s.reason for s in db.scalars(select(Suppression))}


def kinds(db: Session, msg_id: int) -> list[str]:
    db.expire_all()
    return [e.kind for e in messages.list_events(db, msg_id)]


# ------------------------------------------------------------------ reply_status
def test_human_reply_sets_replied_and_last_reply_at(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, snippet="Let's talk")
    replies.poll_all(llm=FakeLLMClient([HUMAN]))
    m = fresh(db, msg)
    assert m.reply_status == "replied" and m.last_reply_at is not None


def test_status_precedence_replied_beats_bounced_beats_auto_reply(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, "p@acme.example", auto_submitted="auto-replied", subject="Automatic reply: hi")
    replies.poll_all(llm=FakeLLMClient([]))
    assert fresh(db, msg).reply_status == "auto_reply"
    reply(team, team.ann, msg, "mailer-daemon@x.example", subject="Undeliverable")
    replies.poll_all(llm=FakeLLMClient([]), force=True)
    assert fresh(db, msg).reply_status == "bounced"
    reply(team, team.ann, msg, snippet="Hello, interested")
    replies.poll_all(llm=FakeLLMClient([HUMAN]), force=True)
    assert fresh(db, msg).reply_status == "replied"
    # a later auto-reply or bounce can never downgrade it
    reply(team, team.ann, msg, "p@acme.example", auto_submitted="auto-replied", subject="Automatic reply: again")
    reply(team, team.ann, msg, "mailer-daemon@x.example", subject="Undeliverable again")
    replies.poll_all(llm=FakeLLMClient([]), force=True)
    assert fresh(db, msg).reply_status == "replied" and len(all_replies(db)) == 5


def test_auto_reply_does_not_stop_follow_ups_or_set_last_reply_at(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    fu = follow_up(db, team, msg, "draft")
    reply(team, team.ann, msg, "p@acme.example", auto_submitted="auto-replied", subject="Automatic reply: away")
    replies.poll_all(llm=FakeLLMClient([]))
    assert fresh(db, fu).state == "draft" and fresh(db, msg).last_reply_at is None
    assert "follow_up_stopped" not in kinds(db, fu.id)


# ------------------------------------------------------------------ follow-ups
@pytest.mark.parametrize("state", ["draft", "edited", "approved"])
def test_a_human_reply_rejects_pending_follow_ups(db: Session, team: Team, state: str) -> None:
    msg = sent_message(db, team, team.ann)
    fu = follow_up(db, team, msg, state)
    if state == "approved":
        fu.content_sha256, fu.approved_by_member_id = "a" * 64, team.ann.id
        db.commit()
    reply(team, team.ann, msg, snippet="Not now")
    replies.poll_all(llm=FakeLLMClient([human("not_now")]))
    f = fresh(db, fu)
    assert f.state == "rejected" and f.content_sha256 is None and f.approved_by_member_id is None
    assert f.error and "reply" in f.error
    assert "follow_up_stopped" in kinds(db, fu.id)


def test_a_sent_follow_up_and_unrelated_messages_are_untouched(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    sent_fu = follow_up(db, team, msg, "sent")
    other = make_message(db, team.ann, to="z@other.example", state="draft")
    other_parent = sent_message(db, team, team.ann, to="q@other.example")
    unrelated_fu = follow_up(db, team, other_parent, "draft")
    reply(team, team.ann, msg, snippet="No thanks")
    replies.poll_all(llm=FakeLLMClient([human("not_now")]))
    assert fresh(db, sent_fu).state == "sent" and fresh(db, other).state == "draft" and fresh(db, unrelated_fu).state == "draft"


def test_a_bounce_also_stops_pending_follow_ups(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    fu = follow_up(db, team, msg, "edited")
    reply(team, team.ann, msg, "mailer-daemon@x.example", subject="Undelivered Mail Returned to Sender", snippet="550 no such user")
    replies.poll_all(llm=FakeLLMClient([]))
    f = fresh(db, fu)
    assert f.state == "rejected" and "bounced" in (f.error or "")


# ------------------------------------------------------------------ suppression effects
def test_unsubscribe_suppresses_the_sender_and_the_original_recipients(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann, to="p@acme.example, Boss@Acme.example")
    reply(team, team.ann, msg, "Assistant@Acme.example", snippet="Please remove me from your list")
    replies.poll_all(llm=FakeLLMClient([human("unsubscribe")]))
    assert suppressed(db) == {"assistant@acme.example": "unsubscribe", "p@acme.example": "unsubscribe", "boss@acme.example": "unsubscribe"}
    (row,) = all_replies(db)
    assert {s.source_reply_id for s in db.scalars(select(Suppression))} == {row.id}


@pytest.mark.parametrize("intent", ["interested", "not_now", "question", "other"])
def test_other_intents_do_not_suppress(db: Session, team: Team, intent: str) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg)
    replies.poll_all(llm=FakeLLMClient([human(intent)]))
    assert suppressed(db) == {}


def test_hard_bounce_suppresses_x_failed_recipients_else_the_original_recipients(db: Session, team: Team) -> None:
    named = sent_message(db, team, team.ann, to="typo@acme.example, fine@acme.example")
    plain = sent_message(db, team, team.ann, to="Gone@Beta.example")
    reply(team, team.ann, named, "mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)", x_failed_recipients="Typo@Acme.example", snippet="550 5.1.1 no such user")
    reply(team, team.ann, plain, "postmaster@beta.example", subject="Undeliverable: Workshop", snippet="Address not found")
    replies.poll_all(llm=FakeLLMClient([]))
    assert suppressed(db) == {"typo@acme.example": "bounce", "gone@beta.example": "bounce"}


def test_soft_bounce_does_not_suppress(db: Session, team: Team) -> None:
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg, "mailer-daemon@googlemail.com", subject="Delivery Status Notification (Delay)", snippet="mailbox full, we will keep trying")
    replies.poll_all(llm=FakeLLMClient([]))
    assert suppressed(db) == {} and fresh(db, msg).reply_status == "bounced"


def test_suppression_add_is_idempotent_and_normalised(db: Session) -> None:
    a = suppressions.add(db, " <Foo@Bar.Example> ", "manual")
    b = suppressions.add(db, "foo@bar.example", "bounce")
    db.commit()
    assert a.id == b.id and a.address == "foo@bar.example" and b.reason == "manual"


# ------------------------------------------------------------------ custom subscribers
def test_custom_subscriber_receives_the_event_with_the_right_fields(db: Session, team: Team) -> None:
    got: list[ReplyReceived] = []
    events.subscribe(ReplyReceived, lambda _db, ev: got.append(ev))
    msg = sent_message(db, team, team.ann)
    bounced = sent_message(db, team, team.ann, to="x@acme.example")
    reply(team, team.ann, msg, "Priya@Acme.example", snippet="send details")
    reply(team, team.ann, bounced, "mailer-daemon@x.example", subject="Undeliverable", x_failed_recipients="x@acme.example")
    replies.poll_all(llm=FakeLLMClient([human("interested")]))
    by_kind = {e.kind: e for e in got}
    h, b = by_kind["human"], by_kind["bounce"]
    (hrow,) = [r for r in all_replies(db) if r.kind == "human"]
    assert (h.outreach_message_id, h.intent, h.member_id, h.from_addr, h.reply_id) == (msg.id, "interested", team.ann.id, "priya@acme.example", hrow.id)
    assert h.failed_recipients == ()
    assert (b.outreach_message_id, b.intent, b.hard_bounce, b.failed_recipients) == (bounced.id, None, True, ("x@acme.example",))


def test_a_failing_subscriber_does_not_stop_others_or_the_poll(db: Session, team: Team) -> None:
    seen: list[int] = []

    def boom(_db: Session, ev: ReplyReceived) -> None:
        raise RuntimeError("subscriber exploded")

    events.subscribe(ReplyReceived, boom)
    events.subscribe(ReplyReceived, lambda _db, ev: seen.append(ev.outreach_message_id))
    a = sent_message(db, team, team.ann)
    b = sent_message(db, team, team.ann, to="q@acme.example")
    reply(team, team.ann, a)
    reply(team, team.ann, b, "q@acme.example")
    (r,) = replies.poll_all(llm=FakeLLMClient([HUMAN, HUMAN]))
    assert r.status == "ok" and r.new_replies == 2
    assert seen == [a.id, b.id] or sorted(seen) == sorted([a.id, b.id])
    assert fresh(db, a).reply_status == "replied" and fresh(db, b).reply_status == "replied"  # default subscriber still ran
    assert len(all_replies(db)) == 2


def test_publish_counts_successful_handlers_and_unsubscribe_works(db: Session) -> None:
    calls: list[int] = []

    def ok(_db: Session, ev: ReplyReceived) -> None:
        calls.append(ev.outreach_message_id)

    events.subscribe(ReplyReceived, ok)
    events.subscribe(ReplyReceived, ok)  # duplicate registration is a no-op
    ev = ReplyReceived(outreach_message_id=999, kind="auto_reply", intent=None)
    assert events.publish(db, ev) == 1 and calls == [999]
    events.unsubscribe(ReplyReceived, ok)
    assert events.publish(db, ev) == 0 and calls == [999]


def test_a_failing_subscriber_rolls_back_only_its_own_writes(db: Session, team: Team) -> None:
    def half_done(_db: Session, ev: ReplyReceived) -> None:
        suppressions.add(_db, "ghost@acme.example", "manual")
        raise RuntimeError("after writing")

    events.subscribe(ReplyReceived, half_done)
    msg = sent_message(db, team, team.ann)
    reply(team, team.ann, msg)
    replies.poll_all(llm=FakeLLMClient([HUMAN]))
    assert "ghost@acme.example" not in suppressed(db) and fresh(db, msg).reply_status == "replied"

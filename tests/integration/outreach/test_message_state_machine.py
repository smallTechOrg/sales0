"""Outreach message state machine against a migrated DB and FakeGmailClient (nothing real is sent)."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import GmailConnection, OutreachMessage
from sales0.errors import DomainError
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.integrations.gmail.errors import GmailApiError, GmailAuthError, GmailTransientError
from sales0.integrations.gmail.mime import parse_raw
from sales0.services.outreach import decks, messages
from sales0.services.team import connections
from tests.integration.outreach.conftest import PDF, REFRESH, Team, make_message


def kinds(db: Session, msg_id: int) -> list[str]:
    return [e.kind for e in messages.list_events(db, msg_id)]


def err_code(exc_info: pytest.ExceptionInfo[DomainError]) -> str:
    return exc_info.value.code


# ------------------------------------------------------------------ happy path & hash binding
def test_approve_then_send_delivers_once_with_the_deck_and_records_everything(db: Session, team: Team, deck) -> None:
    msg = make_message(db, team.ann, deck_id=deck.id)
    messages.approve(db, msg.id, team.ann)
    db.refresh(msg)
    assert msg.state == "approved" and msg.approved_by_member_id == team.ann.id and msg.approved_at
    approved_sha = msg.content_sha256
    assert approved_sha and len(approved_sha) == 64

    out = messages.send(db, msg.id, team.ann)
    assert out.state == "sent" and out.gmail_message_id == "msg-1" and out.gmail_thread_id == "thr-1" and out.sent_at
    gm = team.gmail[team.ann.id]
    assert len(gm.sent) == 1
    sent = parse_raw(gm.sent[0]["raw"])
    assert sent.to == ("p@acme.example",) and sent.subject == "Workshop for Acme"
    assert [(a.filename, a.data) for a in sent.attachments] == [("Workshops Deck.pdf", PDF)]
    assert gm.sent[0]["approved"].sha256 == approved_sha and gm.sent[0]["approved"].approved_by == team.ann.email
    assert kinds(db, msg.id) == ["approved", "sending", "sent"]
    assert all(e.member_id == team.ann.id for e in messages.list_events(db, msg.id))
    assert messages.member_sends_today(db, team.ann.id) == 1


def test_hash_binds_to_deck_bytes_and_a_changed_deck_blocks_the_send(db: Session, team: Team, deck) -> None:
    plain = make_message(db, team.ann)
    with_deck = make_message(db, team.ann, deck_id=deck.id)
    messages.approve(db, plain.id, team.ann)
    messages.approve(db, with_deck.id, team.ann)
    assert plain.content_sha256 != with_deck.content_sha256

    from sales0.config import get_settings

    (get_settings().data_path / deck.stored_path).write_bytes(PDF + b"swapped after approval")
    out = messages.send(db, with_deck.id, team.ann)
    assert out.state == "failed" and out.send_outcome_unknown is False and "does not match its hash" in (out.error or "")
    assert team.gmail[team.ann.id].sent == [] and not any(c[0] == "create_draft" for c in team.gmail[team.ann.id].calls)


# ------------------------------------------------------------------ edit / approve rules
def test_editing_after_approval_resets_it_and_requires_reapproval(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    first = msg.content_sha256
    edited = messages.update_message(db, msg.id, team.ann, body="Hello Priya,\n\nA different pitch.\n\nBest regards,\nAnn Rao")
    assert edited.state == "edited" and edited.content_sha256 is None and edited.approved_at is None
    assert edited.approved_by_member_id is None
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "not_approved"
    assert team.gmail[team.ann.id].calls == []

    messages.approve(db, msg.id, team.ann)
    assert msg.content_sha256 != first
    assert kinds(db, msg.id) == ["approved", "edited", "approval_reset", "approved"]
    assert messages.update_message(db, msg.id, team.ann, subject=msg.subject).state == "approved"  # no-op keeps approval


def test_updates_are_forbidden_after_sending_started(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    messages.send(db, msg.id, team.ann)
    for state in ("sent", "sending"):
        msg.state = state
        db.commit()
        with pytest.raises(DomainError) as exc:
            messages.update_message(db, msg.id, team.ann, body="too late")
        assert err_code(exc) == "not_editable"
    with pytest.raises(DomainError):
        messages.reject(db, msg.id, team.ann)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"body": "Hi {first_name}, hello"}, "unfilled_placeholders"),
        ({"body": "Dear [Your Name],"}, "unfilled_placeholders"),
        ({"subject": "About {{company}}"}, "unfilled_placeholders"),
        ({"to": ""}, "no_recipients"),
        ({"to": "not-an-address"}, "invalid_address"),
        ({"cc": "ok@x.example, bad@@x"}, "invalid_address"),
        ({"subject": "  "}, "empty_message"),
    ],
)
def test_approve_is_blocked_by_bad_content(db: Session, team: Team, kw: dict, code: str) -> None:
    msg = make_message(db, team.ann)
    messages.update_message(db, msg.id, team.ann, **kw)
    with pytest.raises(DomainError) as exc:
        messages.approve(db, msg.id, team.ann)
    assert err_code(exc) == code
    db.refresh(msg)
    assert msg.state == "edited" and msg.content_sha256 is None


def test_only_the_owner_acts_and_admins_can_only_view(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    for actor in (team.bob, team.admin):
        for call in (
            lambda a=actor: messages.approve(db, msg.id, a),
            lambda a=actor: messages.update_message(db, msg.id, a, body="x"),
            lambda a=actor: messages.reject(db, msg.id, a),
        ):
            with pytest.raises(DomainError) as exc:
                call()
            if actor is team.bob:  # another member cannot tell it exists (same as a missing id)
                assert err_code(exc) == "not_found" and exc.value.http_status == 404
            else:  # an admin may read it, so gets the explicit refusal
                assert err_code(exc) == "not_owner" and exc.value.http_status == 403
    messages.approve(db, msg.id, team.ann)
    for actor in (team.bob, team.admin):
        with pytest.raises(DomainError) as exc:
            messages.send(db, msg.id, actor)
        assert err_code(exc) == ("not_found" if actor is team.bob else "not_owner")
        with pytest.raises(DomainError):
            messages.retry_send(db, msg.id, actor, True)
    assert all(g.calls == [] for g in team.gmail.values())

    assert messages.get_message(db, msg.id, team.admin).id == msg.id
    with pytest.raises(DomainError):
        messages.get_message(db, msg.id, team.bob)
    assert [m.id for m in messages.list_outbox(db, team.ann)] == [msg.id]
    assert messages.list_outbox(db, team.bob) == []
    assert messages.list_outbox(db, team.admin) == []
    assert [m.id for m in messages.list_outbox(db, team.admin, everyone=True)] == [msg.id]
    assert [m.id for m in messages.list_outbox(db, None, "approved")] == [msg.id]
    assert messages.list_outbox(db, None, ["sent", "failed"]) == []


def test_reject_is_terminal(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    messages.reject(db, msg.id, team.ann, reason="wrong company")
    db.refresh(msg)
    assert msg.state == "rejected" and msg.content_sha256 is None
    with pytest.raises(DomainError):
        messages.send(db, msg.id, team.ann)
    with pytest.raises(DomainError):
        messages.update_message(db, msg.id, team.ann, body="x")
    assert kinds(db, msg.id) == ["approved", "rejected"]


# ------------------------------------------------------------------ safety checks on send
class TamperingGmail(FakeGmailClient):
    """Someone edits the draft in Gmail between draft creation and send."""

    def create_draft(self, *a, **kw):  # type: ignore[no-untyped-def]
        draft_id = super().create_draft(*a, **kw)
        self.edit_draft_body(draft_id, "Hello - changed behind our back")
        return draft_id


def test_tampered_gmail_draft_is_refused_and_nothing_is_sent(db: Session, team: Team) -> None:
    team.gmail[team.ann.id] = TamperingGmail(email=team.ann.email)
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    out = messages.send(db, msg.id, team.ann)
    gm = team.gmail[team.ann.id]
    assert out.state == "failed" and out.send_outcome_unknown is False
    assert "nothing was sent" in (out.error or "") and gm.sent == [] and gm.raw_drafts == {}
    assert out.content_sha256 is None and out.gmail_draft_id is None  # must be approved again
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "not_approved"


def test_gmail_account_that_is_not_the_sender_is_refused(db: Session, team: Team) -> None:
    team.gmail[team.ann.id] = FakeGmailClient(email="someone.else@gmail.example")
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "sender_mismatch"
    db.refresh(msg)
    assert msg.state == "approved"
    assert [c[0] for c in team.gmail[team.ann.id].calls] == ["profile_email"]


def test_sender_comparison_ignores_case(db: Session, team: Team) -> None:
    team.gmail[team.ann.id] = FakeGmailClient(email="ANN@Team.Example")
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    assert messages.send(db, msg.id, team.ann).state == "sent"


def test_message_from_a_different_address_than_the_member_is_refused(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    msg.from_addr = "other@team.example"
    db.commit()
    messages.approve(db, msg.id, team.ann)
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "sender_mismatch"


def test_member_without_a_usable_connection_cannot_send(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    row = db.query(GmailConnection).filter_by(member_id=team.ann.id).one()
    row.status = "needs_reauth"
    db.commit()
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "gmail_not_connected" and team.gmail[team.ann.id].calls == []
    db.refresh(msg)
    assert msg.state == "approved"


# ------------------------------------------------------------------ idempotency & concurrency
def test_sending_twice_sends_once(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    a = messages.send(db, msg.id, team.ann)
    calls_after_first = list(team.gmail[team.ann.id].calls)
    b = messages.send(db, msg.id, team.ann)
    assert a.state == b.state == "sent" and b.gmail_message_id == "msg-1"
    assert len(team.gmail[team.ann.id].sent) == 1 and team.gmail[team.ann.id].calls == calls_after_first
    db.refresh(msg)
    msg.state = "sending"  # an in-flight message is also returned as is, without touching Gmail
    db.commit()
    assert messages.send(db, msg.id, team.ann).state == "sending"
    assert team.gmail[team.ann.id].calls == calls_after_first


def test_two_threads_clicking_send_at_once_send_exactly_one_email(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    msg_id, gm = msg.id, team.gmail[team.ann.id]
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def click() -> None:
        try:
            with get_sessionmaker()() as session:
                me = session.get(type(team.ann), team.ann.id)
                barrier.wait(timeout=10)
                results.append(messages.send(session, msg_id, me).state)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=click) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors
    assert len(gm.sent) == 1
    assert sorted(results)[-1] in ("sent", "sending") and all(r in ("sent", "sending") for r in results)
    db.expire_all()
    assert db.get(OutreachMessage, msg_id).state == "sent"
    assert kinds(db, msg_id).count("sent") == 1


# ------------------------------------------------------------------ daily cap
def test_daily_cap_is_enforced_per_member_per_local_day(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_GMAIL_DAILY_SEND_CAP", "2")
    reset_settings()
    ids = []
    for i in range(3):
        m = make_message(db, team.ann, subject=f"Subject {i}")
        messages.approve(db, m.id, team.ann)
        ids.append(m.id)
    assert messages.send(db, ids[0], team.ann).state == "sent"
    assert messages.send(db, ids[1], team.ann).state == "sent"
    with pytest.raises(DomainError) as exc:
        messages.send(db, ids[2], team.ann)
    assert err_code(exc) == "daily_cap_reached" and exc.value.http_status == 429
    db.expire_all()
    assert db.get(OutreachMessage, ids[2]).state == "approved"  # untouched, still approved
    assert len(team.gmail[team.ann.id].sent) == 2

    other = make_message(db, team.bob)  # the cap is per member
    messages.approve(db, other.id, team.bob)
    assert messages.send(db, other.id, team.bob).state == "sent"

    tomorrow = utcnow() + timedelta(days=1)
    assert messages.send(db, ids[2], team.ann, now=tomorrow).state == "sent"


def test_cap_of_zero_disables_sending(db: Session, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_GMAIL_DAILY_SEND_CAP", "0")
    reset_settings()
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "daily_cap_reached"


# ------------------------------------------------------------------ failures
class FlakySend(FakeGmailClient):
    def __init__(self, email: str, error: Exception | None) -> None:
        super().__init__(email)
        self.error = error

    def send_draft(self, approved):  # type: ignore[no-untyped-def]
        if self.error is not None:
            self.calls.append(("send_draft", (approved.draft_id,)))
            raise self.error
        return super().send_draft(approved)


def test_ambiguous_send_failure_needs_an_explicit_confirmed_retry(db: Session, team: Team) -> None:
    flaky = FlakySend(team.ann.email, GmailTransientError("Gmail unreachable (ReadTimeout)"))
    team.gmail[team.ann.id] = flaky
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    out = messages.send(db, msg.id, team.ann)
    assert out.state == "failed" and out.send_outcome_unknown is True and "Sent folder" in (out.error or "")
    assert out.gmail_draft_id is not None and len(flaky.raw_drafts) == 1  # the draft is kept for inspection

    sends_before = sum(1 for c in flaky.calls if c[0] == "send_draft")
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)  # a plain send never retries it
    assert err_code(exc) == "outcome_unknown"
    with pytest.raises(DomainError) as exc:
        messages.retry_send(db, msg.id, team.ann, False)
    assert err_code(exc) == "confirmation_required"
    with pytest.raises(DomainError):
        messages.update_message(db, msg.id, team.ann, body="edit while unknown")
    with pytest.raises(DomainError) as exc:
        messages.retry_send(db, msg.id, team.bob, True)
    assert err_code(exc) == "not_found"
    assert sum(1 for c in flaky.calls if c[0] == "send_draft") == sends_before
    db.refresh(msg)
    assert msg.state == "failed" and msg.retry_decided_by is None

    flaky.error = None
    done = messages.retry_send(db, msg.id, team.ann, True)
    assert done.state == "sent" and done.retry_decided_by == team.ann.email and done.send_outcome_unknown is False
    assert len(flaky.sent) == 1 and flaky.raw_drafts == {}  # old draft removed, one email out
    assert kinds(db, msg.id) == ["approved", "sending", "send_failed", "retry_confirmed", "sent"]
    assert messages.retry_send(db, msg.id, team.ann, True).state == "sent"  # idempotent
    assert len(flaky.sent) == 1


def test_clean_rejection_before_send_is_a_known_failure_and_a_click_retries_it(db: Session, team: Team) -> None:
    flaky = FlakySend(team.ann.email, GmailApiError("Gmail HTTP 400: Invalid To header"))
    team.gmail[team.ann.id] = flaky
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    out = messages.send(db, msg.id, team.ann)
    assert out.state == "failed" and out.send_outcome_unknown is False and flaky.sent == [] and flaky.raw_drafts == {}
    flaky.error = None
    assert messages.send(db, msg.id, team.ann).state == "sent"  # deliberate human click
    assert len(flaky.sent) == 1
    with pytest.raises(DomainError) as exc:  # confirmed retries are only for unknown outcomes
        messages.retry_send(db, make_message(db, team.ann).id, team.ann, True)
    assert err_code(exc) == "not_retryable"


def test_auth_rejection_marks_failed_not_unknown_and_flags_the_connection(db: Session, team: Team) -> None:
    team.gmail[team.ann.id] = FlakySend(team.ann.email, GmailAuthError("Google rejected the access token"))
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    out = messages.send(db, msg.id, team.ann)
    assert out.state == "failed" and out.send_outcome_unknown is False
    assert connections.connection_status(db, team.ann.id).status == "needs_reauth"
    with pytest.raises(DomainError) as exc:
        messages.send(db, msg.id, team.ann)
    assert err_code(exc) == "gmail_not_connected"


def test_edit_after_failure_then_approve_and_send(db: Session, team: Team) -> None:
    flaky = FlakySend(team.ann.email, GmailApiError("Gmail HTTP 400"))
    team.gmail[team.ann.id] = flaky
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    assert messages.send(db, msg.id, team.ann).state == "failed"
    messages.update_message(db, msg.id, team.ann, to="q@acme.example")
    flaky.error = None
    db.refresh(msg)
    assert msg.state == "edited" and msg.error is None
    messages.approve(db, msg.id, team.ann)
    assert messages.send(db, msg.id, team.ann).state == "sent"


def test_secrets_never_reach_events_errors_or_logs(db: Session, team: Team, capsys: pytest.CaptureFixture[str]) -> None:
    team.gmail[team.ann.id] = FlakySend(team.ann.email, GmailTransientError("boom"))
    msg = make_message(db, team.ann)
    messages.approve(db, msg.id, team.ann)
    messages.send(db, msg.id, team.ann)
    messages.retry_send(db, msg.id, team.ann, True)
    db.refresh(msg)
    blob = " ".join([msg.error or "", *(e.detail or "" for e in messages.list_events(db, msg.id))])
    captured = capsys.readouterr()
    assert REFRESH not in blob and REFRESH not in captured.out + captured.err
    row = db.query(GmailConnection).filter_by(member_id=team.ann.id).one()
    assert REFRESH not in (row.token_encrypted or "")  # stored encrypted


def test_deck_soft_delete_does_not_break_an_approved_message(db: Session, team: Team, deck) -> None:
    msg = make_message(db, team.ann, deck_id=deck.id)
    messages.approve(db, msg.id, team.ann)
    decks.delete_deck(db, deck.id)
    db.commit()
    assert messages.send(db, msg.id, team.ann).state == "sent"
    with pytest.raises(DomainError):
        messages.update_message(db, make_message(db, team.ann).id, team.ann, deck_id=deck.id)  # cannot attach a deleted deck

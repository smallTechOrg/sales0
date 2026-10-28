"""A suppressed address can neither be drafted to, edited in, approved nor sent to."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from sales0.errors import DomainError
from sales0.services import suppressions
from sales0.services.outreach import drafting, messages
from tests.integration.replies.conftest import Team, make_message
from tests.support.fake_llm import FakeLLMClient

GOOD = {"subject": "AI workshops for Acme Logistics", "body": "Hello Priya,\n\nAcme Logistics 14 warehouses; we run AI workshops.\n\nBest regards,\nAnn Rao"}


def mutating(team: Team) -> list[str]:
    return [c[0] for gm in team.gmail.values() for c in gm.calls if c[0] in {"create_draft", "send_draft", "trash_thread", "delete_draft"}]


def code(exc: pytest.ExceptionInfo[DomainError]) -> str:
    return exc.value.code


def gen(db: Session, team: Team, llm: FakeLLMClient, **kw):  # type: ignore[no-untyped-def]
    return drafting.generate_draft(db, llm, team.ann, first_name="Priya", company="Acme Logistics", requirement="AI training", **{"to": "p@acme.example", **kw})


def test_generate_draft_refuses_a_suppressed_recipient_before_any_llm_call(db: Session, team: Team) -> None:
    suppressions.add(db, "P@Acme.Example", "unsubscribe")
    db.commit()
    llm = FakeLLMClient([GOOD])
    with pytest.raises(DomainError) as exc:
        gen(db, team, llm)
    assert code(exc) == "recipient_suppressed" and exc.value.http_status == 409 and "p@acme.example" in str(exc.value)
    assert llm.calls == 0 and mutating(team) == []


def test_generate_draft_refuses_a_suppressed_cc(db: Session, team: Team) -> None:
    suppressions.add(db, "boss@acme.example", "bounce")
    db.commit()
    with pytest.raises(DomainError) as exc:
        gen(db, team, FakeLLMClient([GOOD]), cc=["Boss@ACME.example"])
    assert code(exc) == "recipient_suppressed"


def test_update_message_refuses_suppressed_to_and_cc(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    suppressions.add(db, "blocked@acme.example", "unsubscribe")
    db.commit()
    for kw in ({"to": ["Blocked@Acme.example"]}, {"to": "p@acme.example, blocked@acme.example"}, {"cc": ["BLOCKED@acme.example"]}):
        with pytest.raises(DomainError) as exc:
            messages.update_message(db, msg.id, team.ann, **kw)
        assert code(exc) == "recipient_suppressed"
    db.refresh(msg)
    assert msg.to_addrs == "p@acme.example" and msg.cc_addrs == "" and msg.state == "draft"
    messages.update_message(db, msg.id, team.ann, subject="A different subject")  # unrelated edits stay allowed
    assert mutating(team) == []


def test_approve_refuses_a_suppressed_recipient_even_in_cc(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann, to="P@Acme.Example")
    other = make_message(db, team.ann, to="ok@acme.example")
    other.cc_addrs = "Cc.Person@Acme.example"
    db.commit()
    suppressions.add(db, "p@acme.example", "bounce")
    suppressions.add(db, "cc.person@acme.example", "unsubscribe")
    db.commit()
    for m in (msg, other):
        with pytest.raises(DomainError) as exc:
            messages.approve(db, m.id, team.ann)
        assert code(exc) == "recipient_suppressed"
        db.refresh(m)
        assert m.state == "draft" and m.content_sha256 is None
    assert mutating(team) == []


def test_send_refuses_an_address_suppressed_after_approval_and_touches_no_gmail_api(db: Session, team: Team) -> None:
    msg = make_message(db, team.ann)
    cc_msg = make_message(db, team.ann, to="ok@acme.example")
    cc_msg.cc_addrs = "late@acme.example"
    db.commit()
    messages.approve(db, msg.id, team.ann)
    messages.approve(db, cc_msg.id, team.ann)
    suppressions.add(db, "p@acme.example", "unsubscribe")
    suppressions.add(db, "LATE@acme.example", "bounce")
    db.commit()
    for m in (msg, cc_msg):
        with pytest.raises(DomainError) as exc:
            messages.send(db, m.id, team.ann)
        assert code(exc) == "recipient_suppressed"
        db.refresh(m)
        assert m.state == "approved"
    assert mutating(team) == [] and team.gmail[team.ann.id].sent == []


def test_removing_the_suppression_allows_it_again(db: Session, team: Team) -> None:
    s = suppressions.add(db, "p@acme.example", "unsubscribe")
    db.commit()
    llm = FakeLLMClient([GOOD])
    with pytest.raises(DomainError):
        gen(db, team, llm)
    suppressions.remove(db, s.id)
    db.commit()
    msg = gen(db, team, llm)
    db.commit()
    messages.approve(db, msg.id, team.ann)
    assert messages.send(db, msg.id, team.ann).state == "sent"
    assert len(team.gmail[team.ann.id].sent) == 1


def test_remove_unknown_suppression_is_a_clean_404(db: Session) -> None:
    with pytest.raises(DomainError) as exc:
        suppressions.remove(db, 12345)
    assert exc.value.http_status == 404


def test_add_rejects_things_that_are_not_addresses(db: Session) -> None:
    with pytest.raises(DomainError) as exc:
        suppressions.add(db, "not-an-address", "manual")
    assert code(exc) == "invalid_address"
    assert suppressions.suppressed_among(db, None, "", "a@b.example") == []

# ruff: noqa: F811, ANN001
"""Reply tracking web pages through the real app: list, filters, handled round trip (plain +
htmx), per-member visibility, suppressions and permissions, outbox badge, header status line,
XSS, and recipient_suppressed surfacing as an error flash."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import OutreachMessage, OutreachReply, ReplyPollState, Suppression
from sales0.jobs.reply_poller import reset_poller
from sales0.llm import set_llm_client
from sales0.web.app import create_app
from tests.integration.outreach.conftest import Team, db, make_message, team  # noqa: F401 - fixtures
from tests.integration.web.conftest import HX
from tests.support.fake_llm import FakeLLMClient

_seq = iter(range(1, 10_000))


@pytest.fixture
def web(team: Team, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SALES0_REPLY_POLL_MINUTES", "10")
    reset_settings()
    set_llm_client(FakeLLMClient(responder=lambda req: {}))
    with TestClient(create_app()) as c:
        yield c
    reset_poller()


def act_as(web: TestClient, member) -> None:  # noqa: ANN001
    assert web.post("/team/acting", data={"member_id": member.id, "next": "/replies"}, follow_redirects=False).status_code == 303


def add_reply(member_id: int, message_id: int, *, kind: str = "human", intent: str | None = "interested",
              snippet: str = "Sounds great, call me", handled: bool = False, from_addr: str = "p@acme.example") -> int:
    n = next(_seq)
    with get_sessionmaker()() as db:
        row = OutreachReply(
            message_id=message_id, member_id=member_id, gmail_message_id=f"g{n}", gmail_thread_id=f"t{n}", from_addr=from_addr,
            received_at=utcnow() - timedelta(minutes=5), snippet=snippet, kind=kind, intent=intent, reason="why: test reason",
            handled=handled,
        )
        db.add(row)
        db.commit()
        return row.id


def sent_message(db, member, **kw) -> OutreachMessage:  # noqa: ANN001
    msg = make_message(db, member, state="sent", **kw)
    return msg


def test_empty_page_renders(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    html = web.get("/replies").text
    assert "No open replies" in html and 'id="check-now"' in html
    assert 'id="replies-unread"' not in html
    assert web.get("/replies/suppressions").status_code == 200


def test_populated_page_and_filters(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    add_reply(team.ann.id, m.id, snippet="human-one")
    add_reply(team.ann.id, m.id, kind="auto_reply", intent=None, snippet="ooo-one")
    add_reply(team.ann.id, m.id, kind="bounce", intent=None, snippet="bounce-one")
    add_reply(team.ann.id, m.id, intent="not_now", snippet="done-one", handled=True)
    act_as(web, team.ann)
    html = web.get("/replies").text
    assert "human-one" in html and "ooo-one" in html and "bounce-one" in html and "done-one" not in html
    assert f"/outreach/messages/{m.id}" in html and "why: test reason" in html and "Mark handled" in html
    assert 'id="replies-unread"' in html  # 1 unhandled human reply
    only = web.get("/replies?kind=auto_reply").text
    assert "ooo-one" in only and "human-one" not in only
    assert "done-one" in web.get("/replies?handled=handled").text
    both = web.get("/replies?handled=all").text
    assert "done-one" in both and "human-one" in both and "Reopen" in both
    assert "done-one" in web.get("/replies?intent=not_now&handled=all").text
    assert "human-one" not in web.get("/replies?intent=not_now&handled=all").text
    m2 = sent_message(db, team.ann, subject="other")
    add_reply(team.ann.id, m2.id, snippet="other-one")
    by_msg = web.get(f"/replies?message={m2.id}").text
    assert "other-one" in by_msg and "human-one" not in by_msg
    assert web.get("/replies?kind=bogus&handled=zzz").status_code == 200


def test_mark_handled_round_trip_plain_and_htmx(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    rid = add_reply(team.ann.id, m.id)
    act_as(web, team.ann)
    resp = web.post(f"/replies/{rid}/handled", data={"handled": "1"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/replies"
    assert "human-one" not in web.get("/replies").text and "Sounds great" not in web.get("/replies").text
    assert "Sounds great" in web.get("/replies?handled=handled").text
    resp = web.post(f"/replies/{rid}/handled", data={"handled": "0"}, headers=HX)
    assert resp.status_code == 200 and "Mark handled" in resp.text and f'id="reply-{rid}"' in resp.text
    resp = web.post(f"/replies/{rid}/handled", data={"handled": "1"}, headers=HX)
    assert "Reopen" in resp.text
    with get_sessionmaker()() as s:
        assert s.get(OutreachReply, rid).handled is True


def test_mark_all_shown_handled(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    ids = [add_reply(team.ann.id, m.id) for _ in range(3)]
    act_as(web, team.ann)
    html = web.get("/replies").text
    assert html.count('name="reply_id"') == 3
    web.post("/replies/handled-all", data={"reply_id": ids, "back": "/replies"}, follow_redirects=False)
    with get_sessionmaker()() as s:
        assert all(s.get(OutreachReply, i).handled for i in ids)


def test_other_members_reply_is_hidden_and_not_handleable(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    rid = add_reply(team.ann.id, m.id, snippet="ann-private-reply")
    act_as(web, team.bob)
    assert "ann-private-reply" not in web.get("/replies?scope=all").text
    hx = web.post(f"/replies/{rid}/handled", data={"handled": "1"}, headers=HX)
    assert f'id="reply-{rid}"' not in hx.text and "ann-private-reply" not in hx.text
    resp = web.post(f"/replies/{rid}/handled", data={"handled": "1"}, follow_redirects=False)
    assert resp.status_code in (303, 404)
    with get_sessionmaker()() as s:
        assert s.get(OutreachReply, rid).handled is False


def test_admin_sees_all_with_scope(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    add_reply(team.ann.id, m.id, snippet="ann-reply-xyz")
    act_as(web, team.admin)
    assert "ann-reply-xyz" not in web.get("/replies").text
    all_html = web.get("/replies?scope=all").text
    assert "ann-reply-xyz" in all_html and "to Ann Rao" in all_html


def test_suppression_add_remove_and_permissions(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    web.post("/replies/suppressions", data={"address": "No@Spam.example", "note": "asked nicely"}, follow_redirects=False)
    html = web.get("/replies/suppressions").text
    assert "no@spam.example" in html and "asked nicely" in html and "Added manually" in html
    assert "never draft to or send to" in html or "will never draft" in html
    with get_sessionmaker()() as s:
        sid = s.query(Suppression).one().id
    # member cannot remove: error flash, row stays
    resp = web.post(f"/replies/suppressions/{sid}/delete", follow_redirects=True)
    assert "Only an admin can remove" in resp.text
    hx = web.post(f"/replies/suppressions/{sid}/delete", headers=HX)  # htmx gets the error toast, not a redirect
    assert "Only an admin can remove" in hx.text
    with get_sessionmaker()() as s:
        assert s.query(Suppression).count() == 1
    # bad address -> flash error
    resp = web.post("/replies/suppressions", data={"address": "nope"}, follow_redirects=True)
    assert "full email address" in resp.text
    act_as(web, team.admin)
    web.post(f"/replies/suppressions/{sid}/delete", follow_redirects=False)
    with get_sessionmaker()() as s:
        assert s.query(Suppression).count() == 0
    assert "Nobody is suppressed" in web.get("/replies/suppressions").text


def test_suppression_with_source_reply_links_to_message(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    rid = add_reply(team.ann.id, m.id, intent="unsubscribe")
    with get_sessionmaker()() as s:
        s.add(Suppression(address="p@acme.example", reason="unsubscribe", source_reply_id=rid))
        s.commit()
    act_as(web, team.ann)
    html = web.get("/replies/suppressions").text
    assert f'href="/outreach/messages/{m.id}"' in html and "Unsubscribed" in html
    act_as(web, team.bob)
    assert f'href="/outreach/messages/{m.id}"' not in web.get("/replies/suppressions").text


def test_outbox_badges_and_message_card(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    a = sent_message(db, team.ann, subject="A-replied")
    b = sent_message(db, team.ann, subject="B-bounced")
    c = sent_message(db, team.ann, subject="C-auto")
    d = sent_message(db, team.ann, subject="D-none")
    for msg, status in ((a, "replied"), (b, "bounced"), (c, "auto_reply")):
        msg.reply_status = status
    db.commit()
    add_reply(team.ann.id, a.id, snippet="reply-for-a")
    add_reply(team.ann.id, b.id, kind="bounce", intent=None, snippet="bounce-for-b")
    add_reply(team.ann.id, c.id, kind="auto_reply", intent=None)
    act_as(web, team.ann)
    html = web.get("/outreach").text
    assert "reply-badge--replied" in html and "reply-badge--bounced" in html and "reply-badge--auto" in html
    assert f"/replies?message={a.id}" in html and "Coming later" not in html
    assert re.search(rf'id="message-{d.id}".*?<span class="muted">-</span>', html, re.S)
    page = web.get(f"/outreach/messages/{a.id}").text
    assert "reply-for-a" in page and 'id="bounce-note"' not in page
    bpage = web.get(f"/outreach/messages/{b.id}").text
    assert 'id="bounce-note"' in bpage and "bounce-for-b" in bpage
    with get_sessionmaker()() as s:
        s.add(Suppression(address="p@acme.example", reason="manual"))
        s.commit()
    assert 'id="suppressed-note"' in web.get(f"/outreach/messages/{d.id}").text


def test_header_status_line_all_levels(web: TestClient, team: Team, monkeypatch: pytest.MonkeyPatch) -> None:
    act_as(web, team.ann)
    assert "replies-status--idle" in web.get("/replies").text
    with get_sessionmaker()() as s:
        s.add(ReplyPollState(member_id=team.ann.id, last_ok_at=utcnow() - timedelta(minutes=3), last_polled_at=utcnow()))
        s.commit()
    html = web.get("/replies").text
    assert "replies-status--ok" in html and "Replies: checked 3m ago" in html and 'href="/replies"' in html
    with get_sessionmaker()() as s:
        st = s.query(ReplyPollState).one()
        st.consecutive_failures = 2
        st.last_error = "token revoked"
        s.commit()
    html = web.get("/replies").text
    assert "replies-status--warn" in html and "token revoked" in html
    monkeypatch.setenv("SALES0_REPLY_POLL_MINUTES", "0")
    reset_settings()
    html = web.get("/replies").text
    assert "replies-status--off" in html and "Replies: off" in html


def test_xss_snippet_is_escaped(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    m = sent_message(db, team.ann)
    add_reply(team.ann.id, m.id, snippet="<script>alert(1)</script>", from_addr="x@a.example")
    act_as(web, team.ann)
    for url in ("/replies", f"/outreach/messages/{m.id}"):
        html = web.get(url).text
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_check_now_flashes_counts(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    resp = web.post("/replies/check", follow_redirects=True)
    assert re.search(r"Checked \d+ conversations?, \d+ new repl", resp.text)


def test_check_now_when_already_running(web: TestClient, team: Team) -> None:
    from sales0.jobs.reply_poller import get_poller

    act_as(web, team.ann)
    poller = get_poller()
    assert poller._cycle_lock.acquire(blocking=False)  # noqa: SLF001
    try:
        resp = web.post("/replies/check", follow_redirects=True)
    finally:
        poller._cycle_lock.release()  # noqa: SLF001
    assert "already running" in resp.text


def test_recipient_suppressed_surfaces_as_error_flash(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    with get_sessionmaker()() as s:
        s.add(Suppression(address="p@acme.example", reason="bounce"))
        s.commit()
    msg = make_message(db, team.ann, to="other@acme.example")
    act_as(web, team.ann)
    resp = web.post(f"/outreach/messages/{msg.id}/edit", data={"to": "p@acme.example", "subject": "s", "body": "b"}, follow_redirects=True)
    assert resp.status_code == 200 and 'class="flash flash--error"' in resp.text and "p@acme.example" in resp.text
    assert "suppress" in resp.text.lower()

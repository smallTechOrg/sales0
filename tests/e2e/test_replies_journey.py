"""Reply tracking end to end: in-process app (fixture LinkedIn), headless Chromium, REAL Haiku
for the ambiguous replies, FakeGmailClient as the mailbox. Nothing real is sent or read."""

from __future__ import annotations

from datetime import timedelta

from playwright.sync_api import Page, expect
from sqlalchemy import select

from sales0.clock import utcnow
from sales0.db.engine import get_sessionmaker
from sales0.db.models import OutreachMessage, OutreachReply, Suppression, TeamMember
from sales0.services import suppressions
from tests.e2e.test_outreach_journey import ADMIN_EMAIL, Env, env, settled  # noqa: F401 - env is a fixture


def _seed_sent(env: Env) -> int:  # noqa: F811
    from sales0.integrations.gmail.threads import ThreadMessage

    with get_sessionmaker()() as db:
        admin = db.execute(select(TeamMember).where(TeamMember.email == ADMIN_EMAIL)).scalar_one()
        sent = utcnow() - timedelta(hours=2)
        msg = OutreachMessage(
            member_id=admin.id, connection_email=ADMIN_EMAIL, from_addr=ADMIN_EMAIL, to_addrs="priya@acme.example",
            subject="AI workshop for Acme", body="Hello", state="sent", gmail_message_id="m-sent", gmail_thread_id="t-1",
            sent_at=sent,
        )  # fmt: skip
        db.add(msg)
        db.commit()
        mid = msg.id
    env.gmail.threads["t-1"] = [
        ThreadMessage(
            id="m-sent", thread_id="t-1", from_addr=ADMIN_EMAIL, from_name="", to_addrs=("priya@acme.example",), cc_addrs=(),
            subject="AI workshop for Acme", date=sent, snippet="Hello", labels=("SENT",),
        )
    ]
    return mid


def test_replies_poll_triage_handle_and_suppress(env: Env, ui: Page) -> None:  # noqa: F811
    page = ui
    mid = _seed_sent(env)
    # Connect the admin's Gmail row (store a token) via the pages used elsewhere is covered by the outreach journey;
    # here the connection row is created directly.
    from sales0.integrations.gmail.auth import SCOPES, StoredToken
    from sales0.services.team import connections

    with get_sessionmaker()() as db:
        admin = db.execute(select(TeamMember).where(TeamMember.email == ADMIN_EMAIL)).scalar_one()
        connections.store_token(db, admin.id, StoredToken("cid", "cs", "1//rt", list(SCOPES), ADMIN_EMAIL))
        db.commit()

    env.gmail.add_reply("t-1", "priya@acme.example", subject="Re: AI workshop for Acme",
                        snippet="Thanks for reaching out. Please remove me from your mailing list and do not contact me again.")
    env.gmail.add_reply("t-1", "mailer-daemon@acme.example", subject="Delivery Status Notification (Failure)",
                        snippet="Address not found", content_type="multipart/report; report-type=delivery-status",
                        x_failed_recipients="priya@acme.example")
    env.gmail.add_reply("t-1", "bob@acme.example", subject="Automatic reply: AI workshop", snippet="I am out of office until Monday.",
                        auto_submitted="auto-replied")

    page.goto(f"{env.base}/replies")
    expect(page.locator("#replies-health")).to_be_visible()
    page.get_by_role("button", name="Check now").click()
    expect(page.locator(".flash--ok")).to_contain_text("new repl")

    page.goto(f"{env.base}/replies?handled=all")
    rows = page.locator("#replies-table tbody tr")
    expect(rows).to_have_count(3)
    expect(page.locator('tr[data-kind="human"]')).to_contain_text("Unsubscribe")
    expect(page.locator('tr[data-kind="bounce"]')).to_have_count(1)
    expect(page.locator('tr[data-kind="auto_reply"]')).to_have_count(1)

    # Second check is idempotent: still three rows.
    page.get_by_role("button", name="Check now").click()
    page.goto(f"{env.base}/replies?handled=all")
    expect(page.locator("#replies-table tbody tr")).to_have_count(3)

    # Mark the human reply handled: it leaves the default (open) view.
    page.goto(f"{env.base}/replies?kind=human")
    page.locator('tr[data-kind="human"]').get_by_role("button", name="Mark handled").click()
    settled(page)
    page.goto(f"{env.base}/replies?kind=human")
    expect(page.locator('tr[data-kind="human"]')).to_have_count(0)

    # Outbox badge + suppression list (unsubscribe + hard bounce => priya suppressed once).
    page.goto(f"{env.base}/outreach")
    expect(page.locator(f"#message-{mid}")).to_contain_text("Replied")
    page.goto(f"{env.base}/replies/suppressions")
    expect(page.locator("#suppressions-table")).to_contain_text("priya@acme.example")

    with get_sessionmaker()() as db:
        assert db.execute(select(OutreachReply)).scalars().all().__len__() == 3
        assert [s.address for s in db.execute(select(Suppression)).scalars()] == ["priya@acme.example"]
        assert suppressions.suppressed_among(db, "PRIYA@acme.example") == ["priya@acme.example"]
        assert db.get(OutreachMessage, mid).reply_status == "replied"
    # Read-only mailbox guarantee.
    assert {c[0] for c in env.gmail.calls} <= {"profile_email", "get_thread", "list_threads_with_new_messages"}

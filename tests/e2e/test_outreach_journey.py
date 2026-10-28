"""Phase-3 outreach, end to end through the real web app and a real browser.

Harness choice (documented): the app runs IN-PROCESS under uvicorn on a free port >= 8766
(fixture LinkedIn mode, temp DB/data dir), driven by headless Chromium. In-process was
chosen over the subprocess ``LiveServer`` because ``connections.set_client_factory`` can
then hand the app a ``FakeGmailClient`` (a subprocess cannot share it, and a fake Gmail REST
server would need a test-only API-base override in production code). Everything else is
real: the OAuth redirect/callback against a tiny fake Google HTTP server (SALES0_GOOGLE_*
endpoints), the research agent against the local fixture website (loopback allowed only via
the fixture-mode-only SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS switch) with REAL Haiku, and the
drafting call with REAL Haiku. NO real email is ever sent.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect
from sqlalchemy import select

from sales0.config import reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import GmailConnection, LlmCall, OutreachEvent, OutreachMessage
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.integrations.gmail.mime import parse_raw
from sales0.jobs.runner import reset_runner
from sales0.services.team import connections
from sales0.web.app import create_app
from tests.integration.outreach.conftest import PDF
from tests.support.fake_google import FakeGoogle, serve_fake_google
from tests.support.research_site import FixtureSite, serve_fixture_site

ADMIN_EMAIL = "admin@team.example"
FIRST_PORT = 8766  # 8765 is the user's own sales0 and is never touched


def free_port(start: int = FIRST_PORT) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


class Env:
    def __init__(self, base: str, google: FakeGoogle, gmail: FakeGmailClient, site: FixtureSite) -> None:
        self.base, self.google, self.gmail, self.site = base, google, gmail, site


@pytest.fixture
def env(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    secret = tmp_path / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "synthetic-id.apps.example", "client_secret": "synthetic-secret"}}))
    port = free_port()
    assert port != 8765
    with serve_fake_google(ADMIN_EMAIL) as google, serve_fixture_site() as site:
        for key, value in {
            "SALES0_LINKEDIN_MODE": "fixture", "SALES0_HOST": "127.0.0.1", "SALES0_PORT": str(port),
            "SALES0_SECRET_KEY": "e2e-secret-key-0123456789abcdef", "SALES0_ADMIN_EMAIL": ADMIN_EMAIL,
            "SALES0_ADMIN_NAME": "Ada Admin", "SALES0_GOOGLE_CLIENT_SECRET_PATH": str(secret),
            "SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS": "1", **google.env(),
        }.items():  # fmt: skip
            monkeypatch.setenv(key, value)
        reset_settings()
        gmail = FakeGmailClient(email=ADMIN_EMAIL)
        connections.set_client_factory(lambda member_id, http: gmail)
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, name="sales0-e2e", daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("sales0 e2e server did not start")
            time.sleep(0.05)
        try:
            yield Env(f"http://127.0.0.1:{port}", google, gmail, site)
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            reset_runner()


def settled(page: Page) -> None:
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')")


def test_connect_gmail_research_draft_edit_approve_send_once(env: Env, ui: Page, tmp_path: Path) -> None:
    page = ui
    page.on("dialog", lambda d: d.accept())
    pdf = tmp_path / "Workshops Deck.pdf"
    pdf.write_bytes(PDF)

    # ---- Team: not connected -> Connect (fake Google authorize -> callback) -> connected
    page.goto(f"{env.base}/team")
    row = page.locator("tr", has_text=ADMIN_EMAIL)
    expect(row.locator("[data-status]")).to_have_attribute("data-status", "not_connected")
    page.get_by_role("button", name="Connect my Gmail").click()
    expect(page.locator(".flash--ok")).to_contain_text(f"Gmail connected as {ADMIN_EMAIL}")
    expect(page.locator("tr", has_text=ADMIN_EMAIL).locator("[data-status]")).to_have_attribute("data-status", "connected")
    assert env.google.paths()[:3] == ["/authorize", "/token", "/userinfo"]

    # ---- Outreach nav is a real page now (not a stub); empty outbox; cap usage
    page.get_by_role("link", name="Outreach", exact=False).first.click()
    expect(page.locator("h1")).to_have_text("Outreach")
    expect(page.locator("#outbox-empty")).to_be_visible()
    expect(page.locator("#cap-usage")).to_contain_text("0 of 40")

    # ---- Decks: upload a PDF mapped to the offering
    page.goto(f"{env.base}/outreach/decks")
    page.set_input_files("#deck-file", str(pdf))
    page.select_option("#deck-offering", label="AI corporate workshops")
    page.get_by_role("button", name="Upload deck").click()
    expect(page.locator(".flash--ok")).to_contain_text("uploaded")
    expect(page.locator("#deck-table")).to_contain_text("Workshops Deck.pdf")

    # ---- Templates: create with a live preview
    page.goto(f"{env.base}/outreach/templates")
    page.fill("#t-name", "Workshop intro")
    page.select_option("#t-offering", label="AI corporate workshops")
    page.fill("#t-subject", "AI workshops for {company}")
    page.fill("#t-body", "Hi {first_name},\n\nWe run AI workshops for teams like {company}.\n\n{sender_name}")
    expect(page.locator("#preview-subject")).to_have_text("AI workshops for Acme Logistics")
    page.fill("#t-body", "Hi {first_name} {bogus}")
    expect(page.locator("#preview-body")).to_contain_text("Unknown placeholder(s): bogus")
    page.fill("#t-body", "Hi {first_name},\n\nWe run AI workshops for teams like {company}.\n\n{sender_name}")
    expect(page.locator("#preview-text")).to_contain_text("Hi Priya,")
    page.get_by_role("button", name="Save template").click()
    expect(page.locator(".flash--ok")).to_contain_text("created")

    # ---- Compose: pick offering -> template + deck auto-selected; research with real Haiku
    page.goto(f"{env.base}/outreach/compose")
    page.fill("#contact_name", "Priya Menon")
    page.fill("#to", "priya@acme.example")
    page.fill("#company", "Acme Logistics")
    page.fill("#website", env.site.base_url)
    page.fill("#requirement", "Two-day GenAI workshop for 40 warehouse and planning managers in Pune")
    page.select_option("#offering_id", label="AI corporate workshops")
    settled(page)
    expect(page.locator("#template_id option:checked")).to_have_text("Workshop intro")
    expect(page.locator("#deck_id option:checked")).to_contain_text("Workshops Deck")

    page.locator("#research-btn").click()
    expect(page.locator("#research-btn")).to_be_disabled()  # clear progress state
    expect(page.locator("#research-indicator")).to_be_visible()
    expect(page.locator("#brief")).to_be_visible(timeout=120_000)
    expect(page.locator("#research-btn")).to_be_enabled()
    expect(page.locator("#brief-sources a").first).to_have_attribute("href", re.compile(re.escape(env.site.base_url)))
    expect(page.locator("#brief-hooks li").first).to_be_visible()
    assert "/" in env.site.hits

    page.locator("#draft-btn").click()
    expect(page.locator("#draft-btn")).to_be_disabled()
    page.wait_for_url(re.compile(r"/outreach/messages/\d+$"), timeout=90_000)
    msg_id = int(page.url.rsplit("/", 1)[1])
    expect(page.locator("#message-hooks li").first).to_be_visible()
    expect(page.locator("#sender-banner")).to_contain_text(f"Sending as {ADMIN_EMAIL}")
    expect(page.locator("#send-btn")).to_have_count(0)  # nothing to send until approved
    expect(page.locator("#deck_id option:checked")).to_contain_text("Workshops Deck")
    assert env.gmail.sent == [] and env.gmail.calls == []

    # ---- Edit: set subject/body (clears any "needs attention" nag), save
    subject = "AI workshops for Acme Logistics - a quick idea"
    body = "Hi Priya,\n\nAcme Logistics runs warehouses, and we run hands-on GenAI workshops for planning teams.\nWould a two-day session in Pune help?\n\nBest regards,\nAda Admin"
    page.fill("#subject", subject)
    page.fill("#body", body)
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator(".flash--ok")).to_contain_text("Saved")

    # ---- Approve (shows the exact hash), then Send
    approve = page.locator("#approve-btn")
    expect(approve).to_contain_text("hash")
    prefix = re.search(r"hash ([0-9a-f]{12})", approve.inner_text()).group(1)  # type: ignore[union-attr]
    approve.click()
    expect(page.locator("#approved-hash")).to_have_text(prefix)
    expect(page.locator("#send-btn")).to_be_visible()
    expect(page.locator("#edit-reset-notice")).to_contain_text("resets its approval")

    page.locator("#send-btn").click()
    expect(page.locator(".flash--ok").first).to_contain_text(f"Sent from {ADMIN_EMAIL}")
    expect(page.locator("#sent-notice")).to_contain_text("cannot be sent again")
    expect(page.locator("#send-btn")).to_have_count(0)
    expect(page.locator("#timeline li")).to_have_count(5)  # drafted, edited, approved, sending, sent

    # ---- Fake Gmail got exactly one send, with the PDF, recipients and approved hash
    assert len(env.gmail.sent) == 1
    sent = env.gmail.sent[0]
    content = parse_raw(sent["raw"])
    assert list(content.to) == ["priya@acme.example"] and content.subject == subject
    assert [a.filename for a in content.attachments] == ["Workshops Deck.pdf"] and content.attachments[0].data == PDF
    assert sent["approved"].sha256.startswith(prefix)

    # ---- DB: sent with ids, timeline events, LLM rows for both features
    with get_sessionmaker()() as db:
        msg = db.get(OutreachMessage, msg_id)
        assert msg is not None and msg.state == "sent" and msg.gmail_message_id == sent["message_id"] and msg.gmail_thread_id
        kinds = [e.kind for e in db.execute(select(OutreachEvent).where(OutreachEvent.message_id == msg_id).order_by(OutreachEvent.id)).scalars()]
        features = {f for (f,) in db.execute(select(LlmCall.feature).where(LlmCall.status == "ok")).all()}
    assert kinds == ["drafted", "edited", "approved", "sending", "sent"]
    assert {"research", "drafting"} <= features

    # ---- Resend is impossible: reload, replay the POST (a "second click"): still one send
    page.reload()
    expect(page.locator("#send-btn")).to_have_count(0)
    replay = page.request.post(f"{env.base}/outreach/messages/{msg_id}/send", headers={"Accept": "application/json"}, max_redirects=0)
    assert replay.status in (303, 200, 409)
    page.reload()
    assert len(env.gmail.sent) == 1
    page.goto(f"{env.base}/outreach")
    expect(page.locator("#cap-usage")).to_contain_text("1 of 40")
    expect(page.locator(f"#message-{msg_id}")).to_have_attribute("data-state", "sent")

    # ---- Team page: Disconnect revokes at Google and shows Disconnected
    page.goto(f"{env.base}/team")
    page.get_by_role("button", name="Disconnect").click()
    expect(page.locator(".flash--ok")).to_contain_text("Gmail disconnected")
    expect(page.locator("tr", has_text=ADMIN_EMAIL).locator("[data-status]")).to_have_attribute("data-status", "disconnected")
    assert "/revoke" in env.google.paths()
    with get_sessionmaker()() as db:
        conn = db.execute(select(GmailConnection)).scalar_one()
        assert conn.status == "disconnected" and conn.token_encrypted is None

"""Phase 2 end to end: login -> Inbox accept -> ONE Pipeline prospect -> note -> Draft email (real Haiku) -> approve -> fake send
-> Contacted + 3 follow-ups -> due today -> fake human reply through the real poller (real Haiku intent) -> Lead -> Proposal sent
-> Won -> Client; Sync page with a synthetic static sheet source; Contacts, Competitors & Groups, Import.

Harness (same as the outreach/replies journeys): the app runs IN-PROCESS under uvicorn (port 8792, else the next free port
>= 8766; never 8765) with the fixture LinkedIn mode, a temp DB/data dir, a fake Google OAuth server, the local fixture company
site and a ``FakeGmailClient`` as the mailbox. Real: headless Chromium, the research and drafting calls (Haiku), the reply
classifier (Haiku), the whole CRM. NOTHING is sent anywhere and Google Sheets are never contacted (``StaticSource``)."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect
from sqlalchemy import func, select

from sales0.clock import local_today
from sales0.config import get_settings, reset_settings
from sales0.db.engine import get_sessionmaker
from sales0.db.models import (
    Client,
    ChannelGroup,
    Competitor,
    Contact,
    Enquiry,
    ImportBatch,
    Lead,
    LinkedinProfile,
    OutreachMessage,
    OutreachReply,
    Prospect,
    TimelineEvent,
)
from sales0.db.models.crm import FollowUp
from sales0.integrations.gmail.auth import SCOPES, StoredToken  # noqa: F401 - parity with the replies journey
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.jobs import sheet_sync_job
from sales0.jobs.runner import reset_runner
from sales0.services.sheetsync import StaticSource
from sales0.services.team import connections
from sales0.web.app import create_app
from tests.e2e._fast import free_port
from tests.support.crm_seed import seed_pending_enquiries
from tests.support.fake_google import FakeGoogle, serve_fake_google
from tests.support.research_site import FixtureSite, serve_fixture_site
from tests.unit.sheetsync import fixtures as sheet_fx

ADMIN_EMAIL = "admin@team.example"
PASSWORD = "journey-pass-123"


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
            "SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS": "1", "SALES0_APP_PASSWORD": PASSWORD,
            "SALES0_SHEET_CLIENT_SOURCING_ID": "client-sourcing-sheet-id", "SALES0_SHEET_METRICS_ID": "metrics-sheet-id", **google.env(),  # sheet sync is opt-in
        }.items():  # fmt: skip
            monkeypatch.setenv(key, value)
        reset_settings()
        gmail = FakeGmailClient(email=ADMIN_EMAIL)
        connections.set_client_factory(lambda member_id, http: gmail)
        sheet_sync_job.set_source_factory(lambda: StaticSource(sheet_fx.all_tabs_csv()))  # synthetic CSV, no network
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, name="sales0-e2e-p2", daemon=True)
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
            sheet_sync_job.set_source_factory(None)
            reset_runner()


def settled(page: Page) -> None:
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')")


def timeline_kinds(page: Page) -> list[str]:
    return page.locator("#timeline li[data-kind]").evaluate_all("els => els.map(e => e.dataset.kind)")


def log_in(page: Page, env: Env, *, target: str = "/pipeline") -> None:
    page.goto(f"{env.base}{target}")
    expect(page).to_have_url(re.compile(r"/login\?next="))
    page.fill("#login-email", ADMIN_EMAIL)
    page.fill("#login-password", "not-the-password")
    page.click("#login-submit")
    expect(page.locator("#login-error")).to_have_text("Wrong email or password.")
    expect(page).to_have_url(re.compile(r"/login"))
    page.fill("#login-email", ADMIN_EMAIL)
    page.fill("#login-password", PASSWORD)
    page.click("#login-submit")
    expect(page).to_have_url(re.compile(re.escape(target) + r"$"))


def test_login_gate_is_enforced_before_anything_else(env: Env) -> None:
    """No browser: plain HTTP. Every page redirects to /login; /api/health and /static stay open."""
    with httpx.Client(base_url=env.base, follow_redirects=False) as c:
        r = c.get("/pipeline")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fpipeline"
        assert c.get("/api/health").status_code == 200
        assert c.get("/login").status_code == 200
        assert c.get("/static/css/app.css").status_code == 200
        assert c.get("/api/pipeline/summary", headers={"Accept": "application/json"}).status_code == 401
        bad = c.post("/login", data={"email": ADMIN_EMAIL, "password": "nope", "next": "/pipeline"})
        assert bad.status_code == 401 and "set-cookie" not in bad.headers
        good = c.post("/login", data={"email": ADMIN_EMAIL, "password": PASSWORD, "next": "/pipeline"})
        assert good.status_code == 303 and good.headers["location"] == "/pipeline" and "set-cookie" in good.headers
        assert c.get("/pipeline").status_code == 200  # the cookie jar now carries the session


def test_enquiry_to_client_journey_with_sync_contacts_and_import(env: Env, ui: Page, tmp_path: Path) -> None:
    page = ui
    page.on("dialog", lambda d: d.accept())
    today = local_today(get_settings().tz)

    # ---- Login (redirect, wrong password, right password lands on the page)
    log_in(page, env, target="/pipeline")
    expect(page.locator("h1")).to_have_text("Pipeline")
    expect(page.locator("#logout")).to_be_visible()

    # ---- Team: connect the admin's Gmail (fake Google) so the poller and the sender have a mailbox
    page.goto(f"{env.base}/team")
    page.get_by_role("button", name="Connect my Gmail").click()
    expect(page.locator(".flash--ok")).to_contain_text(f"Gmail connected as {ADMIN_EMAIL}")

    # ---- Two pending enquiries by ONE LinkedIn author
    with get_sessionmaker()() as db:
        seeded = seed_pending_enquiries(db, count=2)
        db.commit()
    first, second = seeded.enquiry_ids

    # ---- Accept #1 in the Inbox -> ONE prospect in Pipeline (New)
    page.goto(f"{env.base}/inbox")
    page.locator(f"#enquiry-{first}").get_by_role("button", name="Accept", exact=True).click()
    expect(page.locator(f"#enquiry-{first}")).to_have_count(0)
    settled(page)
    page.goto(f"{env.base}/pipeline?tab=prospects")
    expect(page.locator("#pipeline-tabs .tabs__tab--active .count-pill")).to_have_text("1")
    expect(page.locator('.pcard[data-stage="new"]')).to_have_count(1)

    # ---- Accept #2 (same author): still ONE prospect, both enquiries linked (idempotent)
    page.goto(f"{env.base}/inbox")
    page.locator(f"#enquiry-{second}").get_by_role("button", name="Accept", exact=True).click()
    expect(page.locator(f"#enquiry-{second}")).to_have_count(0)
    settled(page)
    with get_sessionmaker()() as db:
        prospects = db.execute(select(Prospect)).scalars().all()
        assert len(prospects) == 1
        pid = prospects[0].id
        assert prospects[0].stage == "new" and prospects[0].linkedin_profile_id == seeded.profile_id
        assert db.get(LinkedinProfile, seeded.profile_id).tracked is True
        assert {e.prospect_id for e in db.execute(select(Enquiry).where(Enquiry.id.in_(seeded.enquiry_ids))).scalars()} == {pid}
        kinds = [t.kind for t in db.execute(select(TimelineEvent).where(TimelineEvent.prospect_id == pid).order_by(TimelineEvent.id)).scalars()]
        assert kinds.count("created") == 1 and kinds.count("enquiry_linked") == 2
        prospect_name = prospects[0].name
    page.goto(f"{env.base}/pipeline?tab=prospects")
    expect(page.locator(".pcard")).to_have_count(1)
    page.locator(f'a[href="/pipeline/prospects/{pid}"]').first.click()
    expect(page.locator("#prospect-title")).to_have_text(prospect_name)
    expect(page.locator("#stage-badge")).to_have_text("New")
    expect(page.locator("#linked-enquiries .reply-item")).to_have_count(2)
    expect(page.locator("#tracked-state")).to_have_text("Tracked")

    # ---- Add a note
    page.fill("#note-body", "Spoke to the L&D head on the phone; wants a Pune date in November.")
    page.get_by_role("button", name="Add note").click()
    expect(page.locator("#timeline")).to_contain_text("wants a Pune date in November")
    assert "note" in timeline_kinds(page)

    # ---- Draft email from the prospect -> outreach compose (prefilled) -> research + draft with real Haiku
    page.locator("#btn-draft").click()
    expect(page).to_have_url(re.compile(rf"/outreach/compose\?prospect_id={pid}"))
    expect(page.locator("#prefill-note")).to_contain_text(f"prospect #{pid}")
    expect(page.locator("#to")).to_have_value("priya@acme.example")
    page.fill("#website", env.site.base_url)
    page.select_option("#offering_id", label="AI corporate workshops")
    settled(page)
    page.locator("#research-btn").click()
    expect(page.locator("#brief")).to_be_visible(timeout=120_000)
    page.locator("#draft-btn").click()
    page.wait_for_url(re.compile(r"/outreach/messages/\d+$"), timeout=90_000)
    msg_id = int(page.url.rsplit("/", 1)[1])
    expect(page.locator("#send-btn")).to_have_count(0)  # not approved yet: nothing to send
    assert env.gmail.sent == []
    with get_sessionmaker()() as db:
        assert db.get(OutreachMessage, msg_id).prospect_id == pid
        assert db.execute(select(func.count()).select_from(FollowUp)).scalar_one() == 0  # an unsent draft changes nothing
        assert db.get(Prospect, pid).stage == "new"

    # ---- Edit, approve, fake-send
    page.fill("#subject", "AI workshops for Acme Logistics - a quick idea")
    page.fill("#body", "Hi Priya,\n\nAcme Logistics runs warehouses; we run hands-on GenAI workshops for planning teams.\nWould a two-day session in Pune help?\n\nBest regards,\nAda Admin")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator(".flash--ok")).to_contain_text("Saved")
    page.locator("#approve-btn").click()
    expect(page.locator("#send-btn")).to_be_visible()
    page.locator("#send-btn").click()
    expect(page.locator(".flash--ok").first).to_contain_text(f"Sent from {ADMIN_EMAIL}")
    assert len(env.gmail.sent) == 1

    # ---- The prospect is Contacted and the follow-ups are scheduled (+3 / +7 / +14 days)
    page.goto(f"{env.base}/pipeline/prospects/{pid}")
    expect(page.locator("#stage-badge")).to_have_text("Contacted")
    expect(page.locator("#follow-ups .due-row")).to_have_count(3)
    assert "email_sent" in timeline_kinds(page)
    expect(page.locator("#prospect-outreach")).to_contain_text("Sent")
    with get_sessionmaker()() as db:
        fus = db.execute(select(FollowUp).where(FollowUp.prospect_id == pid).order_by(FollowUp.due_on)).scalars().all()
        assert [f.kind for f in fus] == ["follow_up_1", "follow_up_2", "final_follow_up"]
        assert [(f.due_on - today).days for f in fus] == [3, 7, 14]
        assert db.get(Prospect, pid).contacted_at is not None

    # ---- A follow-up due today shows under "Due today" (a dated reminder added through the UI: no clock hacking needed)
    page.fill("#fu-due", today.isoformat())
    page.fill("#fu-note", "Call the L&D head")
    page.get_by_role("button", name="Add follow-up").click()
    expect(page.locator("#follow-ups")).to_contain_text("Call the L&D head")
    page.goto(f"{env.base}/pipeline")
    due = page.locator("#due-today")
    expect(due.locator(".due-row")).to_have_count(1)
    expect(page.locator("#due-group-today")).to_have_count(1)
    expect(due).to_contain_text(prospect_name)
    expect(due).to_contain_text("Call the L&D head")
    # the Due filter on the list finds it too
    page.goto(f"{env.base}/pipeline?tab=prospects&due=today")
    expect(page.locator("#pipeline-body")).to_contain_text(prospect_name)

    # ---- A fake human reply (interested) -> real poller + real Haiku -> Lead "Meeting scheduled"
    with get_sessionmaker()() as db:
        thread_id = db.get(OutreachMessage, msg_id).gmail_thread_id
    env.gmail.add_reply(
        thread_id, "priya@acme.example", subject="Re: AI workshops for Acme Logistics - a quick idea",
        snippet="Yes, we are very interested. Please send us a proposal and let's book a call early next week.",
    )
    page.goto(f"{env.base}/replies")
    page.get_by_role("button", name="Check now").click()
    expect(page.locator(".flash--ok")).to_contain_text("1 new reply")
    with get_sessionmaker()() as db:
        reply = db.execute(select(OutreachReply)).scalar_one()
        assert reply.kind == "human" and reply.intent == "interested"
        lead = db.execute(select(Lead)).scalar_one()
        assert lead.stage == "meeting_scheduled" and lead.prospect_id == pid
        lead_id = lead.id
    page.goto(f"{env.base}/pipeline/prospects/{pid}")
    expect(page.locator("#status-badge")).to_contain_text("Converted")
    expect(page.locator("#prospect-replies")).to_contain_text("Interested")
    assert {"reply_received", "lead_created"} <= set(timeline_kinds(page))
    page.locator('#timeline li[data-kind="lead_created"] a').click()  # the timeline links to the new lead
    expect(page).to_have_url(re.compile(rf"/pipeline/leads/{lead_id}$"))
    expect(page.locator("#stage-badge")).to_have_text("Meeting scheduled")

    # ---- Proposal sent (date recorded on the origin enquiry) -> Won -> Client
    page.select_option("#lead-stage", label="Proposal sent")
    page.fill("#lead-value", "250000")
    page.get_by_role("button", name="Update lead").click()
    expect(page.locator("#stage-badge")).to_have_text("Proposal sent")
    with get_sessionmaker()() as db:
        assert db.get(Lead, lead_id).proposal_sent_on == today
        assert db.execute(select(Enquiry.date_proposal_sent).where(Enquiry.prospect_id == pid, Enquiry.date_proposal_sent.is_not(None))).first()
    page.select_option("#lead-stage", label="Won")
    page.get_by_role("button", name="Update lead").click()
    expect(page.locator("#stage-badge")).to_have_text("Won")
    expect(page.locator("#client-created")).to_contain_text("Client created")
    with get_sessionmaker()() as db:
        client = db.execute(select(Client)).scalar_one()
        assert client.lead_id == lead_id and client.status == "active"
        client_id = client.id
    page.locator("#client-created a").click()
    expect(page).to_have_url(re.compile(rf"/pipeline/clients/{client_id}$"))
    page.goto(f"{env.base}/pipeline?tab=clients")
    expect(page.locator("#pipeline-tabs .tabs__tab--active .count-pill")).to_have_text("1")

    # ---- The timeline lists every step with timestamps
    page.goto(f"{env.base}/pipeline/prospects/{pid}")
    kinds = set(timeline_kinds(page))
    assert {"created", "enquiry_linked", "note", "email_sent", "follow_up_added", "reply_received", "lead_created"} <= kinds
    assert page.locator("#timeline li time[datetime]").count() == page.locator("#timeline li[data-kind]").count() > 8

    # ---- Sync page: never synced -> Sync now -> per-tab counts + synced rows -> second run changes nothing
    page.goto(f"{env.base}/sync")
    expect(page.locator("h1")).to_have_text("Google Sheet sync")
    expect(page.locator("#sync-last")).to_contain_text("No sync has run yet")
    expect(page.locator("#sync-now")).to_be_enabled()
    page.locator("#sync-now").click()
    expect(page.locator("#sync-tabs")).to_be_visible(timeout=60_000)
    expect(page.locator("#sync-last-status")).to_have_text("ok", timeout=60_000)
    expect(page.locator("#sync-last")).to_contain_text("1 run so far")
    expect(page.locator('#sync-tabs tr[data-tab="Active Enquiries"]')).to_be_visible()
    first_added = int(page.locator('#sync-tabs tr[data-tab="Active Enquiries"] td').nth(2).inner_text())
    assert first_added >= 5
    expect(page.locator('#sync-tabs tr[data-tab="Competitors"] td').nth(2)).not_to_have_text("0")
    page.locator("#sync-now").click()
    expect(page.locator("#sync-last")).to_contain_text("2 runs so far", timeout=60_000)
    for tab in ("Active Enquiries", "Discarded Enquiries", "Outreach", "Prospects", "Competitors", "Groups"):
        cells = page.locator(f'#sync-tabs tr[data-tab="{tab}"] td')
        assert cells.nth(2).inner_text() == "0" and cells.nth(3).inner_text() == "0" and cells.nth(5).inner_text() == "0", tab  # added/updated/removed
    # the sheet's people are in the Pipeline, marked "Synced from sheet"; its competitors and groups on their page
    page.goto(f"{env.base}/pipeline?tab=prospects&q=New+Person")
    expect(page.locator(".pcard")).to_have_count(1)
    expect(page.locator(".pcard")).to_contain_text("Synced from sheet")
    page.goto(f"{env.base}/competitors")
    expect(page.locator("#competitor-list")).to_contain_text("Rival One")
    expect(page.locator("#competitor-list")).to_contain_text("Synced from sheet")
    expect(page.locator("#group-list")).to_contain_text("AI Folks")
    with get_sessionmaker()() as db:
        assert db.execute(select(func.count()).select_from(Competitor).where(Competitor.name == "Rival One")).scalar_one() == 1
        assert db.execute(select(func.count()).select_from(ChannelGroup)).scalar_one() >= 1
    page.goto(f"{env.base}/sync")
    expect(page.locator("#sync-last")).to_contain_text("2 runs so far")

    # ---- Contacts: search, tag, merge two duplicates
    page.goto(f"{env.base}/contacts")
    page.fill("#contacts-search", "priya")
    expect(page.locator("#contact-list")).to_contain_text("priya@acme.example")
    for email in ("dana.one@dupco.example", "dana.two@dupco.example"):
        page.goto(f"{env.base}/contacts#new-contact")
        page.fill("#nc-full-name", "Dana Dup")
        page.fill("#nc-email", email)
        page.fill("#nc-co", "Dup Co")
        page.get_by_role("button", name="Add contact").click()
        settled(page)
    page.goto(f"{env.base}/contacts?q=Dana")
    expect(page.locator("#contact-list tbody tr")).to_have_count(2)
    page.locator("#contact-list tbody tr a").first.click()
    page.fill("#tag-name", "HR head")
    page.get_by_role("button", name="Add tag").click()
    expect(page.locator("#contact-tags")).to_contain_text("HR head")
    page.goto(f"{env.base}/contacts?tag=HR+head")
    expect(page.locator("#contact-list tbody tr")).to_have_count(1)
    page.goto(f"{env.base}/contacts/duplicates")
    expect(page.locator(".dup-group")).to_have_count(1)
    page.locator(".merge-form button").first.click()
    page.locator("#confirm-dialog").get_by_role("button", name="Yes, continue").click()  # the in-page hx-confirm dialog
    expect(page.locator(".dup-group")).to_have_count(0)  # no duplicates left after the merge
    settled(page)
    page.goto(f"{env.base}/contacts?q=Dana")
    expect(page.locator("#contact-list tbody tr")).to_have_count(1)
    with get_sessionmaker()() as db:
        dana = db.execute(select(Contact).where(Contact.full_name == "Dana Dup")).scalars().all()
        assert len(dana) == 1  # merged
    page.goto(f"{env.base}/companies/{_company_id(prospect_name)}")
    expect(page.locator("h1")).to_be_visible()

    # ---- Competitors & Groups pages render and take a new row
    page.goto(f"{env.base}/competitors")
    expect(page.locator("h1")).to_contain_text("Competitors")
    page.fill("#newc-name", "Rival Manual")
    page.fill("#newc-web", "https://rival-manual.example")
    page.locator("#new-competitor").get_by_role("button", name=re.compile("Add")).click()
    expect(page.locator("#competitor-list")).to_contain_text("Rival Manual")

    # ---- Import: paste a single post (real Haiku triage) + CSV import appear on the Import page
    page.goto(f"{env.base}/import")
    page.fill("#paste-text", "Hello network! Our company, Zenith Foods, wants a 2-day corporate training on Generative AI for 30 managers in Bengaluru this December. Please DM your proposals and rates.")
    page.fill("#paste-author", "Ravi Zenith")
    page.locator("#paste-btn").click()
    expect(page.locator("#paste-result")).to_be_visible(timeout=90_000)
    expect(page.locator("#paste-result")).to_contain_text("Added")
    csv_path = tmp_path / "legacy-contacts.csv"
    csv_path.write_text("Name,Email,Company,Tags\nGita Legacy,gita@legacy.example,Legacy Ltd,alumni\nHari Legacy,hari@legacy.example,Legacy Ltd,alumni\n", encoding="utf-8")
    page.goto(f"{env.base}/import")
    page.set_input_files("#csv-file", str(csv_path))
    page.select_option("#csv-kind", "contacts")
    page.locator("#csv-btn").click()
    expect(page.locator("#sum-new")).to_have_text("2")
    page.goto(f"{env.base}/import")
    expect(page.locator("#batch-list")).to_contain_text("legacy-contacts.csv")
    with get_sessionmaker()() as db:
        assert db.execute(select(func.count()).select_from(ImportBatch).where(ImportBatch.filename == "legacy-contacts.csv")).scalar_one() == 1
        assert db.execute(select(Enquiry).where(Enquiry.raw_text.like("%Zenith Foods%"))).scalar_one().review_status == "pending_review"

    # ---- Log out -> the gate is back
    page.locator("#logout").click()
    expect(page).to_have_url(re.compile(r"/login"))
    page.goto(f"{env.base}/pipeline")
    expect(page).to_have_url(re.compile(r"/login\?next="))


def _company_id(prospect_name: str) -> int:
    from sales0.db.models.crm import Company

    with get_sessionmaker()() as db:
        row = db.execute(select(Company.id).order_by(Company.id)).first()
        assert row is not None, f"no company for {prospect_name}"
        return int(row[0])

"""The company model in a real browser (no LLM): accept an enquiry -> a Prospect company -> a person marks it a Lead ->
an enquiry marked Won makes it a Client -> it can be closed (Lost client) and reopened; the Pipeline has the tabs
Enquiries (default) | Prospects | Leads | Clients; the old per-stage URLs redirect to the matching tab / the company page."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect
from sqlalchemy import select

from sales0.db.engine import get_sessionmaker
from sales0.db.models import Enquiry, Prospect
from tests.e2e.test_phase2_journey import Env, env, log_in, settled  # noqa: F401 - env is a fixture
from tests.support.crm_seed import seed_pending_enquiries


def test_company_stages_end_to_end(env: Env, ui: Page) -> None:  # noqa: F811
    page = ui
    page.on("dialog", lambda d: d.accept())
    log_in(page, env, target="/pipeline")
    expect(page.locator("#pipeline-help")).to_contain_text("only move when someone moves them")

    with get_sessionmaker()() as db:
        seeded = seed_pending_enquiries(db, count=2)
        db.commit()
    first, second = seeded.enquiry_ids

    page.goto(f"{env.base}/inbox")
    page.locator(f"#enquiry-{first}").get_by_role("button", name="Accept", exact=True).click()
    expect(page.locator(f"#enquiry-{first}")).to_have_count(0)
    settled(page)
    page.locator(f"#enquiry-{second}").get_by_role("button", name="Accept", exact=True).click()
    expect(page.locator(f"#enquiry-{second}")).to_have_count(0)
    settled(page)

    # one company, two enquiries, nobody has reached out: on the Enquiries board only (one board per company)
    page.goto(f"{env.base}/pipeline")
    expect(page.locator("#tab-enquiries")).to_have_attribute("aria-current", "page")
    expect(page.locator("#tab-enquiries .count-pill")).to_have_text("2")
    expect(page.locator("#enquiry-board .pcard")).to_have_count(2)
    expect(page.locator('#enquiry-board [data-status="open"]').first).to_be_visible()
    # drag a card onto "Contacted": the move popup asks why, then the company moves to the Prospects board
    page.locator("#enquiry-board .pcard").first.drag_to(page.locator('#move-strip [data-drop-target="prospect:contacted"]'))
    expect(page.locator("#move-dialog")).to_be_visible()
    expect(page.locator("#move-help")).to_contain_text("Contacted")
    page.fill("#move-why", "called them")
    page.locator("#move-submit").click()
    expect(page.locator("#move-dialog")).to_be_hidden()
    expect(page.locator("#enquiry-board .pcard")).to_have_count(0)
    page.locator("#pipeline-tabs").get_by_role("link", name="Prospects").click()
    expect(page.locator('.pcard[data-stage="prospect"]')).to_have_count(1)
    expect(page.locator(".board__col")).to_have_count(5)
    expect(page.locator("#lane-became_lead")).to_have_count(0)
    page.locator(".pcard h3 a").click()
    expect(page).to_have_url(re.compile(r"/companies/\d+$"))
    company_url = page.url
    expect(page.locator("#stage-badge")).to_have_text("Prospect")
    expect(page.locator("#company-help")).to_be_visible()

    # a person decides: the "..." menu, Meeting scheduled, behind a confirmation (a manual override)
    page.locator("#status-menu summary").click()
    page.select_option("#override-target", "lead:meeting_scheduled")
    expect(page.locator("#status-override-form [data-move-help]")).to_contain_text("meeting")
    page.locator("#btn-set-status").click()
    page.locator('#confirm-dialog [value="confirm"]').click()
    expect(page.locator("#stage-badge")).to_have_text("Lead")
    expect(page.locator("#sub-status-badge")).to_have_text("Meeting scheduled")
    page.goto(f"{env.base}/leads")  # the old page is the Leads tab now
    expect(page).to_have_url(re.compile(r"/pipeline\?tab=leads$"))
    expect(page.locator('section[data-stage="meeting_scheduled"] .pcard[data-stage="lead"]')).to_have_count(1)
    page.goto(f"{env.base}/pipeline?tab=prospects")  # a lead is never on the Prospects board
    expect(page.locator(".pcard")).to_have_count(0)

    # an enquiry marked Won makes the company a client
    page.goto(company_url)
    page.locator(f"#enquiry-status-form-{first}").get_by_role("button", name="Won").click()
    expect(page.locator("#lead-suggestion")).to_contain_text("Suggested: Client")  # q25: a person confirms
    page.locator("#lead-suggestion").get_by_role("button", name="Mark as client").click()
    expect(page.locator("#stage-badge")).to_have_text("Client")
    with get_sessionmaker()() as db:
        assert {e.deal_status for e in db.scalars(select(Enquiry).where(Enquiry.id.in_(seeded.enquiry_ids)))} >= {"won"}
        pid = db.scalar(select(Prospect.id))
    page.goto(f"{env.base}/clients")
    expect(page).to_have_url(re.compile(r"/pipeline\?tab=clients$"))
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(1)
    page.goto(f"{env.base}/pipeline/prospects/{pid}")  # a bookmarked prospect URL lands on the company page
    expect(page).to_have_url(re.compile(r"/companies/\d+$"))

    # churned client: close (the "..." menu), it leaves Clients for Companies -> Archived, reopen
    page.locator("#status-menu summary").click()
    page.select_option("#override-target", "closed:lost_client")
    page.locator("#btn-set-status").click()
    page.locator('#confirm-dialog [value="confirm"]').click()
    expect(page.locator("#off-ramp-badge")).to_contain_text("Lost client")
    page.goto(f"{env.base}/pipeline?tab=clients")
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(0)
    page.goto(f"{env.base}/companies?view=archived")
    expect(page.locator("#company-list tbody tr")).to_have_count(1)
    page.goto(company_url)
    page.locator("#btn-reopen").click()
    page.locator('#confirm-dialog [value="confirm"]').click()
    expect(page.locator("#stage-badge")).to_have_text("Client")
    expect(page.locator("#off-ramp-badge")).to_have_count(0)

    # q25: marked client by mistake -> the "..." menu puts it back on the Leads board as if it never was a client
    page.locator("#status-menu summary").click()
    expect(page.locator("#undo-client-hint")).to_be_visible()
    page.select_option("#override-target", "lead:proposal_sent")
    page.fill("#override-why", "marked client by mistake")
    page.locator("#btn-set-status").click()
    page.locator('#confirm-dialog [value="confirm"]').click()
    expect(page.locator("#stage-badge")).to_have_text("Lead")
    expect(page.locator("#sub-status-badge")).to_have_text("Proposal sent")
    expect(page.locator("#timeline")).to_contain_text("Client status undone")
    page.goto(f"{env.base}/pipeline?tab=clients")
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(0)

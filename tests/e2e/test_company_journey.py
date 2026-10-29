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
    expect(page.locator("#pipeline-help")).to_contain_text("accepted enquiry")

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

    # one company, two enquiries: the default tab is Enquiries; the Prospects tab has the cadence columns + Became lead
    page.goto(f"{env.base}/pipeline")
    expect(page.locator("#tab-enquiries")).to_have_attribute("aria-current", "page")
    expect(page.locator("#tab-enquiries .count-pill")).to_have_text("2")
    expect(page.locator("#enquiry-board .pcard")).to_have_count(2)
    expect(page.locator('#enquiry-board [data-status="open"]').first).to_be_visible()
    page.locator("#pipeline-tabs").get_by_role("link", name="Prospects").click()
    expect(page.locator('.pcard[data-stage="prospect"]')).to_have_count(1)
    expect(page.locator(".board__col")).to_have_count(6)
    expect(page.locator("#lane-became_lead")).to_be_visible()
    page.locator(".pcard h3 a").click()
    expect(page).to_have_url(re.compile(r"/companies/\d+$"))
    company_url = page.url
    expect(page.locator("#stage-badge")).to_have_text("Prospect")
    expect(page.locator("#company-help")).to_be_visible()

    # a person decides: Mark as lead (no contact / response preconditions)
    page.locator("#btn-mark-lead").click()
    expect(page.locator("#stage-badge")).to_have_text("Lead")
    expect(page.locator("#sub-status-badge")).to_have_text("Meeting scheduled")
    page.goto(f"{env.base}/leads")  # the old page is the Leads tab now
    expect(page).to_have_url(re.compile(r"/pipeline\?tab=leads$"))
    expect(page.locator('section[data-stage="meeting_scheduled"] .pcard[data-stage="lead"]')).to_have_count(1)
    page.goto(f"{env.base}/pipeline?tab=prospects")  # the new lead shows in the recent Became lead lane only
    expect(page.locator("#lane-became_lead .pcard")).to_have_count(1)

    # an enquiry marked Won makes the company a client
    page.goto(company_url)
    page.locator(f"#enquiry-status-form-{first}").get_by_role("button", name="Won").click()
    expect(page.locator("#stage-badge")).to_have_text("Client")
    with get_sessionmaker()() as db:
        assert {e.deal_status for e in db.scalars(select(Enquiry).where(Enquiry.id.in_(seeded.enquiry_ids)))} >= {"won"}
        pid = db.scalar(select(Prospect.id))
    page.goto(f"{env.base}/clients")
    expect(page).to_have_url(re.compile(r"/pipeline\?tab=clients$"))
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(1)
    page.goto(f"{env.base}/pipeline/prospects/{pid}")  # a bookmarked prospect URL lands on the company page
    expect(page).to_have_url(re.compile(r"/companies/\d+$"))

    # churned client: close, see it in the Closed lane, reopen
    page.select_option("#off-ramp-select", "lost_client")
    page.locator("#btn-close").click()
    expect(page.locator("#off-ramp-badge")).to_contain_text("Lost client")
    page.goto(f"{env.base}/pipeline?tab=clients")
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(0)
    page.goto(f"{env.base}/pipeline?tab=clients&closed=1")
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(1)
    page.goto(company_url)
    page.locator("#btn-reopen").click()
    expect(page.locator("#stage-badge")).to_have_text("Client")
    expect(page.locator("#off-ramp-badge")).to_have_count(0)

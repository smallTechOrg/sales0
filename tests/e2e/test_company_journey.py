"""The company model in a real browser (no LLM): accept an enquiry -> a Prospect company -> a person marks it a Lead ->
an enquiry marked Won makes it a Client -> it can be closed (Lost client) and reopened; the Pipeline toggles between
companies and enquiries; the old per-stage URLs redirect to the company page / filtered pipeline."""

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
    expect(page.locator("#pipeline-help")).to_contain_text("Every company we deal with")

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

    # one company, two enquiries; the board has the three stage columns and the Closed lane only when needed
    page.goto(f"{env.base}/pipeline")
    expect(page.locator('.pcard[data-stage="prospect"]')).to_have_count(1)
    expect(page.locator(".board__col")).to_have_count(3)
    page.locator("#pipeline-view").get_by_role("link", name="Enquiries", exact=True).click()
    expect(page.locator("#enquiry-board .pcard")).to_have_count(2)
    expect(page.locator('#enquiry-board [data-status="open"]').first).to_be_visible()
    page.locator("#pipeline-view").get_by_role("link", name="Companies", exact=True).click()
    page.locator(".pcard h3 a").click()
    expect(page).to_have_url(re.compile(r"/companies/\d+$"))
    company_url = page.url
    expect(page.locator("#stage-badge")).to_have_text("Prospect")
    expect(page.locator("#company-help")).to_be_visible()

    # a person decides: Mark as lead (no contact / response preconditions)
    page.locator("#btn-mark-lead").click()
    expect(page.locator("#stage-badge")).to_have_text("Lead")
    expect(page.locator("#sub-status-badge")).to_have_text("Meeting scheduled")
    page.goto(f"{env.base}/leads")  # the old page is a filtered pipeline now
    expect(page).to_have_url(re.compile(r"/pipeline\?stage=lead$"))
    expect(page.locator("#pipeline-list tbody tr")).to_have_count(1)

    # an enquiry marked Won makes the company a client
    page.goto(company_url)
    page.locator(f"#enquiry-status-form-{first}").get_by_role("button", name="Won").click()
    expect(page.locator("#stage-badge")).to_have_text("Client")
    with get_sessionmaker()() as db:
        assert {e.deal_status for e in db.scalars(select(Enquiry).where(Enquiry.id.in_(seeded.enquiry_ids)))} >= {"won"}
        pid = db.scalar(select(Prospect.id))
    page.goto(f"{env.base}/clients")
    expect(page).to_have_url(re.compile(r"/pipeline\?stage=client$"))
    page.goto(f"{env.base}/pipeline/prospects/{pid}")  # a bookmarked prospect URL lands on the company page
    expect(page).to_have_url(re.compile(r"/companies/\d+$"))

    # churned client: close, see it in the Closed lane, reopen
    page.select_option("#off-ramp-select", "lost_client")
    page.locator("#btn-close").click()
    expect(page.locator("#off-ramp-badge")).to_contain_text("Lost client")
    page.goto(f"{env.base}/pipeline")
    expect(page.locator("#closed-lane")).to_be_visible()
    page.goto(company_url)
    page.locator("#btn-reopen").click()
    expect(page.locator("#stage-badge")).to_have_text("Client")
    expect(page.locator("#off-ramp-badge")).to_have_count(0)

"""Phase-4 ops through the real server + browser: spend-cap hard stop on a real run (real Haiku),
the cost view, the schedule page (enable / next run / run now with a recorded skip reason) and the
daily digest page. The server uses its own port (8791) so it never collides with other suites."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e.conftest import _free_port
from tests.e2e._flows import add_keyword, connect_linkedin, run_now
from tests.e2e._fast import live_server

# One real triage call costs ~$0.0002+ (Haiku); a $0.0001 cap is exceeded by the FIRST call, so the
# second triage is refused and the remaining enquiries stay pending.
CAP_ENV = {
    "SALES0_MONTHLY_SPEND_CAP_USD": "0.0001",
    "SALES0_DIGEST_AI_SUMMARY": "false",
}


def test_spend_cap_stops_triage_costs_schedule_and_digest_pages(tmp_path: Path, ui: Page) -> None:
    with live_server(tmp_path / "ops", port=_free_port(8791), extra_env=CAP_ENV) as server:
        page, base = ui, server.base_url

        # Empty cost view first: the cap is configured but nothing is spent yet.
        page.goto(f"{base}/costs")
        expect(page.locator("#cap-line")).to_contain_text("of $0.0001 used")
        expect(page.locator("#spend-cap-banner")).to_have_count(0)

        connect_linkedin(page, base)
        page.goto(f"{base}/setup")
        for kw in ("corporate ai training", "ai workshop", "ai consulting"):
            add_keyword(page, kw)
        run_id, _ = run_now(page, base)

        # Hard stop: the run stopped, said why, and left enquiries pending (not triaged, not lost).
        page.goto(f"{base}/runs/{run_id}")
        expect(page.locator("main")).to_contain_text("spend cap")
        page.goto(f"{base}/inbox")
        expect(page.locator("#spend-cap-banner")).to_be_visible()
        expect(page.locator("#spend-cap-banner")).to_contain_text("Monthly AI spend cap reached")
        # Phase 1.4: posts the cap stopped stay `pending` in seen_posts (no enquiry is created until triaged),
        # so there is nothing to list in the inbox; the integration tests assert the pending rows.

        # Cost view: month spend > 0 from the one allowed real call, cap exceeded, per-feature row.
        page.goto(f"{base}/costs")
        expect(page.locator("#cap-state")).to_have_attribute("data-state", "exceeded")
        expect(page.locator('#feature-table tr[data-feature="triage"]')).to_have_count(1)
        assert page.locator("#month-cost").inner_text().startswith("$0.0")

        # Schedule: enable the nightly job -> a next-run time inside 01:00-05:00; run now is refused with the reason.
        page.goto(f"{base}/schedule")
        job = page.locator("#job-discovery_run")
        expect(job).to_have_attribute("data-enabled", "0")
        job.get_by_role("button", name="Enable").click()
        expect(page.locator("#job-discovery_run")).to_have_attribute("data-enabled", "1")
        text = page.locator("#job-discovery_run .next-run").inner_text()
        assert re.search(r"\d{2} \w{3} \d{4}, 0[1-4]:\d{2}", text), text
        page.locator("#job-discovery_run").get_by_role("button", name="Run now").click()
        expect(page.locator("#job-discovery_run .last-result")).to_contain_text("skipped")
        expect(page.locator("#job-discovery_run .last-result")).to_contain_text("spend cap")

        # Digest: build now (no AI summary configured), shows the cost-today and review sections.
        page.goto(f"{base}/schedule")
        page.locator("#job-daily_digest").get_by_role("button", name="Run now").click()
        expect(page.locator("#schedule-flash")).to_contain_text("Built the digest")
        page.goto(f"{base}/digest")
        expect(page.locator("main")).to_contain_text("AI cost")

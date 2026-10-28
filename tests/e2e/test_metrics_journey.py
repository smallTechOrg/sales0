"""Metrics dashboard through the real server + browser: a real (fixture-LinkedIn, real Haiku) run lands in
this week's computed figures, the manual figure form works, the merge toggle changes the labelled
rule, and a CSV export downloads. Own port range so it never collides with the other suites."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

from tests.e2e._flows import add_keyword, connect_linkedin, run_now
from tests.e2e.conftest import _free_port
from tests.support.live_server import live_server


def test_metrics_reflect_a_real_run_and_manual_entry(tmp_path: Path, ui: Page) -> None:
    with live_server(tmp_path / "metrics", port=_free_port(8830), extra_env={"SALES0_DIGEST_AI_SUMMARY": "false"}) as server:
        page, base = ui, server.base_url

        page.goto(f"{base}/metrics")
        expect(page.locator("#metrics-empty")).to_be_visible()  # fresh DB: empty state, page still complete
        expect(page.locator("#weekly-table")).to_be_visible()

        connect_linkedin(page, base)
        page.goto(f"{base}/setup")
        for kw in ("corporate ai training", "ai workshop"):
            add_keyword(page, kw)
        run_now(page, base)

        page.goto(f"{base}/metrics?weeks=4")
        expect(page.locator("#metrics-empty")).to_have_count(0)
        added = page.locator('#weekly-table tr[data-row="enquiries_added"] td').nth(3)  # last of 4 weeks = this week
        expect(added).to_have_attribute("data-source", "computed")
        assert int(added.inner_text().split()[0]) > 0
        scanned = page.locator('#funnel-table tr[data-stage="scanned"] td').first.inner_text()
        assert int(scanned) > 0
        expect(page.locator("#keyword-table tr[data-keyword]").first).to_be_visible()
        expect(page.locator("svg.chart__svg").first).to_be_visible()  # inline charts render offline

        # manual figure -> labelled "manual"
        page.select_option('#manual-form select[name="metric"]', "posts_published")
        page.fill('#manual-form input[name="value"]', "3")
        page.click("#manual-save")
        expect(page.locator("#metrics-flash")).to_contain_text("Saved")
        cell = page.locator('#weekly-table tr[data-row="posts_published"] td').nth(3)
        expect(cell).to_have_attribute("data-source", "manual")

        # toggle the merge rule: the rule text changes
        page.select_option("#mode-select", "sheet")
        expect(page.locator("#merge-rule")).to_contain_text("sheet's")

        # CSV download
        with page.expect_download() as dl:
            page.click("#export-weekly")
        assert dl.value.suggested_filename == "sales0-metrics-weekly.csv"
        body = Path(dl.value.path()).read_text()
        assert body.startswith("Metric,") and "Number of Enquiries added" in body

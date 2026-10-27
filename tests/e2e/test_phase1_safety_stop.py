"""Account-safety stops in the browser (roadmap gate assertion 5), on its OWN live server
instance (architecture.md → E2E harness): a checkpoint page aborts the run, flags the
session, shows the banner everywhere and pauses Run now; no later keyword is searched and
no AI call is made. Reconnecting clears it; a captcha page then aborts the next run too."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e._flows import add_keyword, connect_linkedin, remove_keyword, run_now
from tests.support.live_server import LiveServer


def test_checkpoint_aborts_run_with_banner_then_reconnect_and_captcha_abort(server: LiveServer, ui: Page) -> None:
    page, base = ui, server.base_url

    page.goto(f"{base}/setup")
    add_keyword(page, "checkpoint-test")
    add_keyword(page, "ai consulting")  # must NOT be searched after the stop
    connect_linkedin(page, base)

    run_id, _ = run_now(page, base)
    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-status", "aborted")
    expect(panel).to_contain_text("Run stopped: LinkedIn checkpoint detected")

    detail = server.get_json(f"/api/runs/{run_id}")
    assert detail["run"]["status"] == "aborted" and detail["run"]["abort_reason"] == "checkpoint", detail["run"]
    assert detail["run"]["posts_seen"] == 0 and detail["run"]["enquiries_triaged"] == 0
    by_kw = {k["keyword_text"]: k for k in detail["keywords"]}
    assert by_kw["ai consulting"]["status"] == "skipped" and by_kw["ai consulting"]["posts_seen"] == 0, by_kw
    assert server.get_json("/api/session")["status"] == "needs_attention"

    # The banner is on every page, cannot be dismissed, and Run now is paused.
    banner = page.locator("#alert-banner")
    for path in ("/runs", "/inbox", "/setup"):
        page.goto(f"{base}{path}")
        expect(banner).to_be_visible()
        expect(banner).to_have_attribute("role", "alert")
        expect(banner).to_contain_text("LinkedIn needs attention")
        expect(banner).to_contain_text("Runs are paused to protect your account.")
        expect(page.locator("#status-light")).to_have_attribute("data-status", "needs_attention")
    expect(page.locator("#linkedin-card")).to_have_attribute("data-status", "needs_attention")
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_disabled()
    expect(page.locator("#run-history")).to_contain_text("Aborted")
    page.goto(f"{base}/runs/{run_id}")
    expect(page.locator("#run-keywords")).to_contain_text("Not searched")

    assert not [e for e in server.log_events() if e.get("event") == "llm_call"], "no AI call may happen on a stopped run"

    # Recovery: Connect again clears the banner; a captcha page aborts the next run.
    connect_linkedin(page, base)
    expect(page.locator("#alert-banner")).to_have_count(0)
    page.goto(f"{base}/setup")
    remove_keyword(page, "checkpoint-test")
    remove_keyword(page, "ai consulting")
    add_keyword(page, "captcha-test")
    run2_id, _ = run_now(page, base)
    expect(page.locator("#run-panel")).to_have_attribute("data-status", "aborted")
    expect(page.locator("#run-panel")).to_contain_text(re.compile(r"Run stopped: .*captcha", re.I))
    assert server.get_json(f"/api/runs/{run2_id}")["run"]["abort_reason"] == "captcha"
    page.goto(f"{base}/inbox")
    expect(page.locator("#alert-banner")).to_contain_text("LinkedIn needs attention")

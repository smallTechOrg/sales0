"""Account-safety stops in the browser (Phase 1 gate assertion 5; Phase 1.1 gate assertion 4),
on its OWN live server instance (architecture.md → E2E harness; cool-downs 0 here).

checkpoint-test aborts the run → session ``needs_attention``, the banner on every page, Run
now paused, no later keyword searched, no AI call, the tripping keyword stamped. A headless
Re-check clears it. The second run then plans the stamped keyword LAST (the other keyword is
searched and triaged first) and trips the checkpoint again. ``logout-test`` (302 → /authwall,
cookie kept) and ``captcha-test`` abort with their own reasons too."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e._flows import (
    add_keyword,
    assert_server_logs_clean,
    connect_linkedin,
    keyword_row,
    recheck_linkedin,
    remove_keyword,
    run_now,
)
from tests.support.live_server import LiveServer


def _llm_calls(server: LiveServer) -> list[dict]:
    return [e for e in server.log_events() if e.get("event") == "llm_call"]


def _assert_needs_attention_everywhere(page: Page, base: str) -> None:
    banner = page.locator("#alert-banner")
    for path in ("/runs", "/inbox", "/setup"):
        page.goto(f"{base}{path}")
        expect(banner).to_be_visible()
        expect(banner).to_have_attribute("role", "alert")
        expect(banner).to_contain_text("LinkedIn needs attention")
        expect(banner).to_contain_text("Runs are paused to protect your account.")
        expect(page.locator("#status-light")).to_have_attribute("data-status", "needs_attention")
    expect(page.locator("#linkedin-card")).to_have_attribute("data-status", "needs_attention")
    expect(page.locator("#linkedin-card")).to_contain_text(
        "Press Connect LinkedIn — the sales0 Chrome window opens on LinkedIn so you can resolve it there."
    )
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_disabled()
    expect(page.locator("#run-now-reason")).to_have_attribute("data-reason-code", "needs_attention")


def _assert_cleared(page: Page, base: str, server: LiveServer) -> None:
    expect(page.locator("#alert-banner")).to_have_count(0)
    session = server.get_json("/api/session")
    assert session["status"] == "connected" and session["cooldown_until"] is None, session  # cool-downs are 0 here
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_enabled()


def test_checkpoint_stop_stamps_keyword_recheck_clears_then_rerun_logout_and_captcha_stops(
    server: LiveServer, ui: Page
) -> None:
    page, base = ui, server.base_url

    page.goto(f"{base}/setup")
    add_keyword(page, "checkpoint-test")
    add_keyword(page, "ai consulting")  # must NOT be searched after the stop
    connect_linkedin(page, base)

    # --- run 1: the checkpoint trips on the first keyword
    run_id, _ = run_now(page, base)
    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-status", "aborted")
    expect(panel).to_contain_text("Run stopped: LinkedIn checkpoint detected")

    detail = server.get_json(f"/api/runs/{run_id}")
    assert detail["run"]["status"] == "aborted" and detail["run"]["abort_reason"] == "checkpoint", detail["run"]
    assert detail["run"]["posts_seen"] == 0 and detail["run"]["enquiries_triaged"] == 0
    assert [k["keyword_text"] for k in detail["keywords"]] == ["checkpoint-test", "ai consulting"]
    by_kw = {k["keyword_text"]: k for k in detail["keywords"]}
    assert by_kw["ai consulting"]["status"] == "skipped" and by_kw["ai consulting"]["posts_seen"] == 0, by_kw
    assert server.get_json("/api/session")["status"] == "needs_attention"

    _assert_needs_attention_everywhere(page, base)
    expect(page.locator("#run-history")).to_contain_text("Aborted")
    page.goto(f"{base}/runs/{run_id}")
    expect(page.locator("#run-keywords")).to_contain_text("Not searched")
    assert not _llm_calls(server), "no AI call may happen on a run stopped before any post"

    # The tripping keyword is stamped with the stop time; the skipped one is not.
    page.goto(f"{base}/setup")
    expect(keyword_row(page, "checkpoint-test")).to_contain_text("Last run")
    expect(keyword_row(page, "ai consulting")).to_contain_text("Not run yet")

    # --- a headless Re-check clears needs_attention (cool-down 0 in tests → Run now allowed)
    check = recheck_linkedin(page, server)
    _assert_cleared(page, base, server)
    assert check["result"] == "connected" and check["headless"] is True, check

    # --- run 2: the stamped keyword is planned LAST; "ai consulting" is searched + triaged first
    run2_id, _ = run_now(page, base)
    expect(panel).to_have_attribute("data-status", "aborted")
    expect(panel).to_contain_text("Run stopped: LinkedIn checkpoint detected")
    run2 = server.get_json(f"/api/runs/{run2_id}")
    assert run2["run"]["abort_reason"] == "checkpoint", run2["run"]
    assert [k["keyword_text"] for k in run2["keywords"]] == ["ai consulting", "checkpoint-test"], run2["keywords"]
    first = run2["keywords"][0]
    assert first["status"] in ("done", "warning") and first["posts_seen"] == 2, first
    assert run2["run"]["posts_seen"] == 2, run2["run"]
    triage_calls = [c for c in _llm_calls(server) if c.get("feature") == "triage"]
    assert triage_calls, "posts collected before the stop are still triaged (no LinkedIn request involved)"
    _assert_needs_attention_everywhere(page, base)

    # --- logout-test: the session "expires" mid-run (302 → /authwall, cookie kept) → logout
    recheck_linkedin(page, server)
    _assert_cleared(page, base, server)
    page.goto(f"{base}/setup")
    remove_keyword(page, "checkpoint-test")
    remove_keyword(page, "ai consulting")
    add_keyword(page, "logout-test")
    run3_id, _ = run_now(page, base)
    expect(panel).to_have_attribute("data-status", "aborted")
    expect(panel).to_contain_text("Run stopped: LinkedIn logged out (the session expired)")
    run3 = server.get_json(f"/api/runs/{run3_id}")
    assert run3["run"]["abort_reason"] == "logout", run3["run"]
    _assert_needs_attention_everywhere(page, base)
    page.goto(f"{base}/setup")
    expect(keyword_row(page, "logout-test")).to_contain_text("Last run")

    # --- captcha-test: a captcha page aborts the next run as well
    recheck_linkedin(page, server)
    _assert_cleared(page, base, server)
    page.goto(f"{base}/setup")
    remove_keyword(page, "logout-test")
    add_keyword(page, "captcha-test")
    run4_id, _ = run_now(page, base)
    expect(panel).to_have_attribute("data-status", "aborted")
    expect(panel).to_contain_text(re.compile(r"Run stopped: .*captcha", re.I))
    assert server.get_json(f"/api/runs/{run4_id}")["run"]["abort_reason"] == "captcha"
    page.goto(f"{base}/inbox")
    expect(page.locator("#alert-banner")).to_contain_text("LinkedIn needs attention")

    # --- logs: LLM + request lines, never a cookie value
    log_text = server.log_text()
    assert re.search(r'"event":\s*"llm_call"', log_text)
    assert_server_logs_clean(server)

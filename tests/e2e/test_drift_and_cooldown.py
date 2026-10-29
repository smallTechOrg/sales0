"""Layout drift is always visible, and a safety stop starts a cool-down that outlives the
green light (Phase 1.1 gate assertions 5 and 6), end to end on a live server with the
production cool-down values (60 / 15 min) instead of the harness's 0.

layout-drift-test → keyword ``warning``, ``layout_warnings=1``, ``#run-drift-alert`` in the
panel and the run detail, "Layout warning" in the history, runs stay allowed. Then
ratelimit-test → aborted ``rate_limited`` → needs_attention (checked first) → a headless
Re-check turns the light green but Run now stays disabled with "LinkedIn asked us to slow
down — next run allowed after HH:MM" → still so after a server restart."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from sales0.services.runs import cooldown_clock
from tests.e2e._flows import (
    add_keyword,
    assert_server_logs_clean,
    connect_linkedin,
    remove_keyword,
    run_now,
)
from tests.e2e.conftest import _free_port
from tests.e2e._fast import live_server
from tests.support.live_server import LiveServer

COOLDOWN_ENV = {"SALES0_SAFETY_COOLDOWN_MIN": "60", "SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN": "15"}
DRIFT_ALERT = (
    "LinkedIn's page layout may have changed — 1 keyword(s) returned results sales0 couldn't fully read. "
    "Some posts may be missing. See the per-keyword log."
)


@pytest.fixture
def cooldown_server(tmp_path: Path) -> Iterator[LiveServer]:
    with live_server(tmp_path / "e2e-cooldown", port=_free_port(), extra_env=COOLDOWN_ENV) as srv:
        yield srv


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def test_drift_alert_then_rate_limit_cooldown_survives_recheck_and_restart(cooldown_server: LiveServer, ui: Page) -> None:
    server, page = cooldown_server, ui
    base = server.base_url

    # Safety limits show the configured cool-downs and the Phase 1.1 browser rows.
    page.goto(f"{base}/setup")
    body = page.locator("body")
    expect(body).to_contain_text("Cool-down after rate limit / captcha")
    expect(body).to_contain_text("60 min")
    expect(body).to_contain_text("Cool-down after checkpoint / logout")
    expect(body).to_contain_text("15 min")
    expect(body).not_to_contain_text("Browser window during runs")

    add_keyword(page, "layout-drift-test")
    add_keyword(page, "ai consulting")
    add_keyword(page, "legacy-urn-test")
    connect_linkedin(page, base)

    # --- run 1: one keyword's results page can't be read → drift alert, runs stay allowed
    run_id, _ = run_now(page, base)
    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-status", "succeeded")
    alert = panel.locator("#run-drift-alert")
    expect(alert).to_be_visible()
    expect(alert).to_have_attribute("role", "status")
    expect(alert).to_have_class(re.compile(r"\bbanner--warn\b"))
    expect(alert).to_contain_text(DRIFT_ALERT)
    expect(alert.locator(f'a[href="/runs/{run_id}#run-keywords"]')).to_have_count(1)
    expect(page.locator("#run-now")).to_be_enabled()

    detail = server.get_json(f"/api/runs/{run_id}")
    assert detail["run"]["status"] == "succeeded" and detail["run"]["layout_warnings"] == 1, detail["run"]
    by_kw = {k["keyword_text"]: k for k in detail["keywords"]}
    drift_row = by_kw["layout-drift-test"]
    assert drift_row["status"] == "warning", drift_row
    assert drift_row["error_message"] and "No posts parsed" in drift_row["error_message"], drift_row
    assert by_kw["ai consulting"]["status"] == "done" and by_kw["ai consulting"]["posts_seen"] == 2, by_kw
    # Phase 1.2: the Phase 1.1 URN card shapes still parse (fallback strategy), all 10 corpus posts.
    assert by_kw["legacy-urn-test"]["status"] == "done" and by_kw["legacy-urn-test"]["posts_seen"] == 10, by_kw
    lines = {e["keyword"]: e for e in server.log_events() if e.get("event") == "scrape_keyword"}
    assert lines["legacy-urn-test"]["strategy"] == "urn", lines["legacy-urn-test"]
    assert lines["ai consulting"]["strategy"] == "sdui", lines["ai consulting"]
    assert lines["layout-drift-test"]["status"] == "warning" and lines["layout-drift-test"]["drift"] is True, lines

    page.goto(f"{base}/runs")
    expect(page.locator("#run-history")).to_contain_text("Layout warning")
    page.goto(f"{base}/runs/{run_id}")
    expect(page.locator("#run-drift-alert")).to_contain_text(DRIFT_ALERT)
    expect(page.locator("#run-keywords")).to_contain_text("No posts parsed")

    # --- run 2: a rate limit stops the run and starts the 60-minute cool-down
    page.goto(f"{base}/setup")
    remove_keyword(page, "layout-drift-test")
    remove_keyword(page, "ai consulting")
    remove_keyword(page, "legacy-urn-test")
    add_keyword(page, "ratelimit-test")
    before = datetime.now(timezone.utc)
    run2_id, _ = run_now(page, base)
    expect(panel).to_have_attribute("data-status", "aborted")
    expect(panel).to_contain_text("Run stopped: LinkedIn rate limit detected")
    assert server.get_json(f"/api/runs/{run2_id}")["run"]["abort_reason"] == "rate_limited"

    session = server.get_json("/api/session")
    assert session["status"] == "needs_attention", session
    assert session["cooldown_until"], session
    until = _utc(session["cooldown_until"])
    assert before + timedelta(minutes=59) <= until <= datetime.now(timezone.utc) + timedelta(minutes=61), until
    clock = cooldown_clock(until)
    assert re.fullmatch(r"\d{2}:\d{2}( tomorrow)?", clock), clock

    # needs_attention wins first; the panel's outcome carries the cool-down line.
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_disabled()
    expect(page.locator("#run-now-reason")).to_have_attribute("data-reason-code", "needs_attention")
    expect(panel).to_contain_text(f"Next run allowed after {clock}")
    page.goto(f"{base}/setup")
    expect(page.locator("#linkedin-card")).to_contain_text(f"After that, the next run is allowed after {clock}.")

    # --- fixscrape D #4: Re-check is refused during the cool-down (it is an extra feed load); Connect resolves it
    page.goto(f"{base}/setup")
    page.locator("#check-linkedin").click()
    expect(page.locator("#toasts")).to_contain_text("Re-check is paused for")
    assert server.get_json("/api/session")["status"] == "needs_attention"
    # --- a green Connect clears needs_attention but NOT the cool-down
    connect_linkedin(page, base)
    expect(page.locator("#alert-banner")).to_have_count(0)
    assert _utc(server.get_json("/api/session")["cooldown_until"]) == until
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_disabled()
    reason = page.locator("#run-now-reason")
    expect(reason).to_have_attribute("data-reason-code", "safety_cooldown")
    expect(reason).to_have_text(f"LinkedIn asked us to slow down — next run allowed after {clock}")

    # --- state survival: the cool-down outlives a server restart
    server.restart()
    page.goto(f"{base}/runs")
    expect(page.locator("#status-light")).to_have_attribute("data-status", "connected")
    expect(page.locator("#run-now")).to_be_disabled()
    expect(page.locator("#run-now-reason")).to_have_attribute("data-reason-code", "safety_cooldown")
    expect(page.locator("#run-now-reason")).to_contain_text("LinkedIn asked us to slow down — next run allowed after")

    # --- logs: real Haiku triage of the drift run's readable keyword, never a cookie value
    assert any(e.get("event") == "llm_call" and e.get("status") == "ok" for e in server.log_events())
    assert_server_logs_clean(server)

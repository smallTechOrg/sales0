"""Shared browser steps for the E2E journeys (no assertions on business outcomes here —
only the waits a real user would do)."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, expect

from sales0.linkedin_fixture.corpus import FixturePost, post_by_id
from sales0.linkedin_fixture.routes import SESSION_COOKIE, SESSION_VALUE
from tests.support.live_server import LiveServer

RUN_TIMEOUT_MS = 300_000
TERMINAL_RUN_STATUSES = ("succeeded", "aborted", "failed")
# ui.md → card link label by ``link_kind`` (Phase 1.2).
LINK_LABELS = {
    "post": "View post ↗",
    "profile": "Author profile (post link unavailable) ↗",
}


def fixture_post(post_id: str) -> FixturePost:
    post = post_by_id(post_id)
    assert post is not None, f"no corpus post {post_id}"
    return post


def card(page: Page, post_id: str) -> Locator:
    """The inbox card whose first source is the corpus post ``post_id``.

    SDUI pages carry no URN, so a card's ``data-urn`` (its first ``source_ref``) is the URN
    only when "Copy link to post" yielded it, else ``ck:<componentkey>``: match any of the
    post's ``FixturePost.possible_refs()``."""
    refs = fixture_post(post_id).possible_refs()
    return page.locator(", ".join(f'#inbox-list article.card[data-urn="{ref}"]' for ref in refs))


def settled(page: Page) -> None:
    """Wait until htmx finished swapping/settling, so new elements have listeners."""
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')")


def tab_count(page: Page, tab: str) -> Locator:
    return page.locator(f'#inbox-tabs [data-count="{tab}"]')


def add_keyword(page: Page, text: str) -> None:
    page.fill("#keyword-input", text)
    page.get_by_role("button", name="Add keyword").click()
    expect(page.locator("#keyword-list")).to_contain_text(text)
    expect(page.locator("#keyword-input")).to_have_value("")
    settled(page)


def remove_keyword(page: Page, text: str) -> None:
    row = page.locator("#keyword-list li").filter(has=page.locator("strong", has_text=text)).first
    row.get_by_role("button", name="Remove").click()
    page.locator("#confirm-dialog").get_by_role("button", name="Yes, continue").click()
    expect(page.locator("#keyword-list")).not_to_contain_text(text)
    settled(page)


def connect_linkedin(page: Page, base_url: str) -> None:
    """Setup → Connect LinkedIn (fixture login: login → 2FA → remember-browser → feed,
    headless in fixture mode) → card + header light turn green."""
    page.goto(f"{base_url}/setup")
    page.locator("#connect-linkedin").click()
    expect(page.locator("#linkedin-card")).to_have_attribute("data-status", "connected", timeout=60_000)
    light = page.locator("#status-light")
    expect(light).to_have_attribute("data-status", "connected", timeout=15_000)
    expect(light).to_have_class(re.compile(r"status-light--connected"))
    expect(light).to_contain_text("LinkedIn connected")


def recheck_linkedin(page: Page, server: LiveServer) -> dict[str, Any]:
    """Setup → Re-check (headless session check) → card + header light turn green.

    Returns the check's ``linkedin_session`` log line. Waits for that line, not just the card:
    a card that was already green would otherwise satisfy the wait before the check ran."""
    checks_before = len(session_events(server, "check"))
    page.goto(f"{server.base_url}/setup")
    page.locator("#check-linkedin").click()
    deadline = time.monotonic() + 60
    while len(session_events(server, "check")) <= checks_before:
        assert time.monotonic() < deadline, "Re-check never logged a linkedin_session line"
        page.wait_for_timeout(50)
    expect(page.locator("#linkedin-card")).to_have_attribute("data-status", "connected", timeout=60_000)
    expect(page.locator("#status-light")).to_have_attribute("data-status", "connected", timeout=15_000)
    return session_events(server, "check")[-1]


def keyword_row(page: Page, text: str) -> Locator:
    return page.locator("#keyword-list li").filter(has=page.locator("strong", has_text=text)).first


def run_now(page: Page, base_url: str, *, timeout_ms: int = RUN_TIMEOUT_MS) -> tuple[int, dict[str, Any]]:
    """Runs → Run now; observe live progress; wait for the outcome.

    Returns (run_id, the in-progress observation)."""
    page.goto(f"{base_url}/runs")
    run_button = page.locator("#run-now")
    expect(run_button).to_be_enabled()
    run_button.click()
    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-run-id", re.compile(r"^\d+$"))
    run_id = int(panel.get_attribute("data-run-id") or "0")
    progress: dict[str, Any] = {}
    status = panel.get_attribute("data-status")
    if status not in TERMINAL_RUN_STATUSES:
        expect(panel).to_contain_text("Run in progress")
        expect(page.locator("#status-light")).to_contain_text("Run in progress", timeout=15_000)
        progress = {
            "title": panel.locator("#run-panel-title").inner_text(),
            "has_progress_bar": panel.locator("progress#run-progress").count() == 1,
            "counters": " ".join(panel.locator(".counters").all_inner_texts()),
        }
    page.wait_for_function(
        "(statuses) => { const p = document.querySelector('#run-panel');"
        " return p && statuses.includes(p.dataset.status) && !p.hasAttribute('hx-get'); }",
        arg=list(TERMINAL_RUN_STATUSES),
        timeout=timeout_ms,
    )
    return run_id, progress


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


FIXTURE = "/__fixtures/linkedin"
# The fixture's 2FA login, in the order the headless Connect window must walk it.
LOGIN_FLOW_PATHS = [
    f"{FIXTURE}/login",
    f"{FIXTURE}/checkpoint/challenge/verify",
    f"{FIXTURE}/checkpoint/challenge/verify-submit",
    f"{FIXTURE}/checkpoint/challenge/remember-browser",
    f"{FIXTURE}/feed/",
]


def session_events(server: LiveServer, action: str | None = None) -> list[dict[str, Any]]:
    """The server's ``linkedin_session`` log lines (connect / check), optionally by action."""
    return [
        e
        for e in server.log_events()
        if e.get("event") == "linkedin_session" and (action is None or e.get("action") == action)
    ]


def assert_server_logs_clean(server: LiveServer) -> None:
    """Every server log carries the request + LLM lines and never a session cookie value."""
    text = server.log_text()
    assert re.search(r'"event":\s*"http_request"', text), "no http_request lines in the server log"
    assert SESSION_VALUE not in text, f"the {SESSION_COOKIE} cookie value leaked into the server log"
    assert not re.search(rf"{SESSION_COOKIE}=[^;\s\"]+", text), f"a {SESSION_COOKIE}=<value> pair is in the server log"
    for path in server.root.glob("server-*.stdout.log"):
        assert SESSION_VALUE not in path.read_text(encoding="utf-8", errors="replace"), (
            f"the {SESSION_COOKIE} cookie value leaked into {path.name}"
        )

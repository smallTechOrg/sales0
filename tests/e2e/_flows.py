"""Shared browser steps for the E2E journeys (no assertions on business outcomes here —
only the waits a real user would do)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, expect

from sales0.config import REPO_ROOT

RUN_TIMEOUT_MS = 300_000
TERMINAL_RUN_STATUSES = ("succeeded", "aborted", "failed")
CORPUS = {p["id"]: p for p in json.loads((REPO_ROOT / "src/sales0/linkedin_fixture/posts.json").read_text(encoding="utf-8"))}


def urn(post_id: str) -> str:
    return CORPUS[post_id]["urn"]


def card(page: Page, post_id: str) -> Locator:
    """The inbox card whose first source is the corpus post ``post_id``."""
    return page.locator(f'#inbox-list article.card[data-urn="{urn(post_id)}"]')


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
    """Setup → Connect LinkedIn (fixture login) → card + header light turn green."""
    page.goto(f"{base_url}/setup")
    page.locator("#connect-linkedin").click()
    expect(page.locator("#linkedin-card")).to_have_attribute("data-status", "connected", timeout=60_000)
    light = page.locator("#status-light")
    expect(light).to_have_attribute("data-status", "connected", timeout=15_000)
    expect(light).to_have_class(re.compile(r"status-light--connected"))
    expect(light).to_contain_text("LinkedIn connected")


def run_now(page: Page, base_url: str) -> tuple[int, dict[str, Any]]:
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
            "counters": panel.locator(".counters").inner_text(),
        }
    page.wait_for_function(
        "(statuses) => { const p = document.querySelector('#run-panel');"
        " return p && statuses.includes(p.dataset.status) && !p.hasAttribute('hx-get'); }",
        arg=list(TERMINAL_RUN_STATUSES),
        timeout=RUN_TIMEOUT_MS,
    )
    return run_id, progress


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

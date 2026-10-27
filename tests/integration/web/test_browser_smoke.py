"""Live-server smoke in headless Chromium for the JS-driven paths (htmx swaps, app.js
toasts, the hx-confirm dialog, bulk select). Real server, real SQLite, no LLM call:
inbox cards are seeded as already-triaged rows."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.integration.web.conftest import make_enquiry, make_run, triaged

pytestmark = pytest.mark.browser


def _settled(page: Page) -> None:
    """Wait until htmx has finished swapping/settling, so new elements have listeners."""
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')")


def test_setup_add_and_remove_keyword_via_htmx_with_confirm_dialog(fixture_server: str, page: Page) -> None:
    page.goto(f"{fixture_server}/setup")
    expect(page.locator("#keyword-list")).to_contain_text("No keywords yet")
    page.fill("#keyword-input", "  AI   workshop ")
    page.get_by_role("button", name="Add keyword").click()
    expect(page.locator("#keyword-list")).to_contain_text("AI workshop")
    expect(page.locator("#toasts .toast--ok")).to_contain_text("Added keyword 'AI workshop'.")
    expect(page.locator("#keyword-input")).to_have_value("")

    page.fill("#keyword-input", "ai workshop")
    page.get_by_role("button", name="Add keyword").click()
    expect(page.locator("#toasts .toast--error")).to_contain_text("is already in your keywords.")
    expect(page.locator("#keyword-input")).to_have_value("ai workshop")  # kept on error so it can be fixed

    page.locator("#keyword-list").get_by_role("switch").click()
    expect(page.locator("#keyword-list").get_by_role("switch")).to_have_attribute("aria-checked", "false")

    page.locator("#keyword-list").get_by_role("button", name="Remove").click()
    dialog = page.locator("#confirm-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Remove keyword 'AI workshop'?")
    dialog.get_by_role("button", name="Yes, continue").click()
    expect(page.locator("#keyword-list")).to_contain_text("No keywords yet")


def test_inbox_bulk_select_discard_and_priority_change(fixture_server: str, page: Page) -> None:
    run_id = make_run("succeeded")
    p1 = make_enquiry("P1", run_id=run_id, **triaged("genuine", "Buyer wants a GenAI workshop", summary="Acme - GenAI workshop - Pune"))
    p2 = make_enquiry("P2", run_id=run_id, **triaged("discard", "Agency recruiting freelance trainers", priority="low", offering_id=None))
    p3 = make_enquiry("P3", run_id=run_id, **triaged("discard", "Seeks an individual trainer", priority="low", offering_id=None))

    page.goto(f"{fixture_server}/inbox")
    expect(page.locator("#inbox-tabs")).to_contain_text("To review (3)")
    bar = page.locator("#bulk-bar")
    expect(bar).not_to_have_class(re.compile("bulk-bar--active"))
    page.locator(f"#enquiry-{p2} input.js-select").check()
    page.locator(f"#enquiry-{p3} input.js-select").check()
    expect(bar).to_have_class(re.compile("bulk-bar--active"))
    expect(bar).to_contain_text("2 selected")
    page.select_option("#bulk-discard-reason", "Looking for individual trainers")
    bar.get_by_role("button", name="Discard selected").click()
    expect(page.locator("#toasts")).to_contain_text("Discarded 2")
    expect(page.locator("#inbox-tabs")).to_contain_text("To review (1)")
    expect(page.locator("#inbox-tabs")).to_contain_text("Discarded (2)")
    expect(page.locator(f"#enquiry-{p2}")).to_have_count(0)
    _settled(page)

    page.select_option(f"#enquiry-{p1}-priority", "medium")
    expect(page.locator("#toasts")).to_contain_text("Priority updated.")
    page.reload()
    expect(page.locator(f"#enquiry-{p1}-priority")).to_have_value("medium")

    page.locator(f"#enquiry-{p1}").get_by_role("button", name="Accept").click()
    expect(page.locator("#inbox-tabs")).to_contain_text("Accepted (1)")
    expect(page.locator("#inbox-list")).to_contain_text("All caught up — nothing to review.")

    page.goto(f"{fixture_server}/inbox?tab=discarded")
    page.fill("#inbox-search", "freelance")
    page.keyboard.press("Enter")
    expect(page.locator("#inbox-list article")).to_have_count(2)

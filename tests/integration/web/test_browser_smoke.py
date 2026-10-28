"""Live-server smoke in headless Chromium for the JS-driven paths (htmx swaps, app.js
toasts, the hx-confirm dialog, bulk select). Real server, real SQLite, no LLM call:
inbox cards are seeded as already-triaged rows."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.integration.web.conftest import make_enquiry, make_run, make_seen_post, triaged

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


def test_setup_add_suggested_keywords_then_edit_one_inline(fixture_server: str, page: Page) -> None:
    page.goto(f"{fixture_server}/setup")
    page.locator("#add-suggested-keywords").click()
    expect(page.locator("#toasts .toast--ok")).to_contain_text("Added 60 · 0 already present")
    expect(page.locator("#keyword-list details.keyword-group")).to_have_count(4)
    expect(page.locator("#keyword-list [data-keyword-id]")).to_have_count(60)
    page.locator("#add-suggested-keywords").click()
    expect(page.locator("#toasts")).to_contain_text("Added 0 · 60 already present")

    row = page.locator("#keyword-list [data-keyword-id]").first
    row.locator("summary", has_text="Edit").click()
    row.locator("input[name=text]").fill("my edited buyer phrase")
    row.get_by_role("button", name="Save").click()
    expect(page.locator("#keyword-list")).to_contain_text("my edited buyer phrase")
    expect(page.locator("#toasts")).to_contain_text("Saved keyword 'my edited buyer phrase'.")


def test_skipped_tab_search_rescue_and_override(fixture_server: str, page: Page) -> None:
    make_seen_post("Agency recruiting freelance trainers for Pune batches", author="Zed Agency")
    make_seen_post("Thought leadership on the future of AI at work", author="Amy Writer", reason="Opinion piece")
    page.goto(f"{fixture_server}/inbox")
    page.locator("#inbox-tab-skipped").click()
    expect(page.locator("#skipped-list article.skipped")).to_have_count(2)
    page.fill("#inbox-search", "zed")
    page.keyboard.press("Enter")
    expect(page.locator("#skipped-list article.skipped")).to_have_count(1)
    page.locator(".skipped__rescue").click()
    expect(page.locator("#toasts")).to_contain_text("Moved to To review")
    expect(page.locator("#inbox-tabs")).to_contain_text("To review (1)")
    expect(page.locator("#inbox-tabs")).to_contain_text("Skipped by AI (1)")
    page.goto(f"{fixture_server}/inbox?tab=skipped")
    page.locator(".skipped__override").click()
    expect(page.locator("#toasts")).to_contain_text("Accepted as Prospect · New (overrode AI)")
    expect(page.locator("#inbox-tabs")).to_contain_text("Accepted (1)")
    expect(page.locator("#skipped-list")).to_contain_text("Nothing skipped yet")

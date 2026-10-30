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
    page.locator("#setup-tabs").get_by_role("link", name="Offerings & keywords").click()
    expect(page.locator("#keyword-results")).to_contain_text("No keywords yet - add some above")
    page.fill("#kw-add-texts", "  AI   workshop ")
    page.locator("#kw-add-form").get_by_role("button", name="Add").click()
    expect(page.locator("#toasts .toast--ok")).to_contain_text("Added 1 keyword.")
    expect(page.locator("#keyword-results")).to_contain_text("AI workshop")
    page.fill("#kw-add-texts", "ai workshop")
    page.locator("#kw-add-form").get_by_role("button", name="Add").click()
    expect(page.locator("#toasts")).to_contain_text("Added 0 keywords · 1 already there.")

    row = page.locator("tr", has_text="AI workshop")
    row.get_by_role("button", name="Pause AI workshop").click()
    expect(page.locator("tr", has_text="AI workshop")).to_have_attribute("data-active", "false")

    page.locator("tr", has_text="AI workshop").locator(".kw-pick").check()
    page.locator("#kw-bulk-form").get_by_role("button", name="Remove").click()
    dialog = page.locator("#confirm-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Remove the ticked keywords?")
    dialog.get_by_role("button", name="Yes, continue").click()
    expect(page.locator("#toasts")).to_contain_text("Removed 1 keyword.")
    expect(page.locator("#keyword-results")).not_to_contain_text("AI workshop")


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
    page.goto(f"{fixture_server}/keywords")
    page.locator("#add-suggested-keywords").click()
    expect(page.locator("#toasts .toast--ok")).to_contain_text("Added 60 starter keywords · 0 already present")
    expect(page.locator("#keyword-results tr[data-tier]")).to_have_count(60)
    page.locator("#add-suggested-keywords").click()
    expect(page.locator("#toasts")).to_contain_text("Added 0 starter keywords · 60 already present")

    row = page.locator("#keyword-results tr[data-tier]").first
    row.locator("summary", has_text="Edit").click()
    row.get_by_label("Keyword text").fill("my edited buyer phrase")
    row.get_by_role("button", name="Save").click()
    expect(page.locator("#keyword-results")).to_contain_text("my edited buyer phrase")
    expect(page.locator("#toasts")).to_contain_text("Saved 'my edited buyer phrase'.")


def test_offering_card_edit_and_rule_in_place(fixture_server: str, page: Page) -> None:
    page.goto(f"{fixture_server}/keywords")
    card = page.locator("section.keyword-tier", has=page.locator(".offering-card")).first
    name = card.get_attribute("aria-label")
    card.get_by_text("Criteria for this offering").click()
    card.get_by_label(f"New rule for {name}").fill("A company wants a hands-on GenAI workshop for its staff.")
    card.get_by_role("button", name=f"Add rule for {name}").click()
    expect(page.locator("#toasts")).to_contain_text(f"Added include rule for {name}.")
    card = page.locator(f'section.keyword-tier[aria-label="{name}"]')
    expect(card).to_contain_text("Criteria for this offering (1 active)")
    card.get_by_text("Edit offering").click()
    card.locator("textarea[name=description]").fill("Two-day hands-on GenAI workshops for teams")
    card.get_by_role("button", name="Save offering").click()
    expect(page.locator("#toasts")).to_contain_text(f"Saved offering '{name}'.")
    expect(page.locator(f'section.keyword-tier[aria-label="{name}"]')).to_contain_text("Two-day hands-on GenAI workshops for teams")


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


def test_keywords_page_bulk_select_inline_edit_and_ai_review_flow(fixture_server: str, page: Page) -> None:
    from sales0.db.engine import session_scope
    from sales0.services import keywords as kw

    with session_scope() as db:
        for text in ("ai workshop", "genai training", "dead phrase"):
            kw.add_keyword(db, text)
    page.goto(f"{fixture_server}/keywords")
    expect(page.locator("#keyword-results")).to_contain_text("ai workshop")
    # "All shown" ticks every row checkbox (CSP-safe delegated handler), then Pause applies to all of them
    page.get_by_label("Tick every keyword shown").check()
    expect(page.locator(".kw-pick:checked")).to_have_count(3)
    page.locator("#kw-bulk-form").get_by_role("button", name="Pause").click()
    expect(page.locator("#toasts .toast--ok")).to_contain_text("Paused 3 keywords.")
    expect(page.locator('tr[data-active="false"]')).to_have_count(3)
    # one row: inline edit sends only that row's fields
    row = page.locator("tr", has_text="genai training")
    row.get_by_text("Edit").click()
    row.get_by_label("Keyword text").fill("GenAI training for HR")
    row.get_by_role("button", name="Save").click()
    expect(page.locator("#toasts")).to_contain_text("Saved 'GenAI training for HR'.")
    expect(page.locator("#keyword-results")).to_contain_text("GenAI training for HR")
    expect(page.locator("#keyword-results")).to_contain_text("ai workshop")
    # bulk add, then remove one with the confirm dialog
    page.fill("#kw-add-texts", "hire ai trainer\nai workshop")
    page.locator("#kw-add-form").get_by_role("button", name="Add").click()
    expect(page.locator("#toasts")).to_contain_text("Added 1 keyword · 1 already there.")
    page.locator("tr", has_text="dead phrase").locator(".kw-pick").check()
    page.locator("#kw-bulk-form").get_by_role("button", name="Remove").click()
    dialog = page.locator("#confirm-dialog")
    expect(dialog).to_contain_text("Remove the ticked keywords?")
    dialog.get_by_role("button", name="Yes, continue").click()
    expect(page.locator("#toasts")).to_contain_text("Removed 1 keyword.")
    expect(page.locator("#keyword-results")).not_to_contain_text("dead phrase")

"""LinkedIn accounts in the browser (Phase 5): the paste-li_at-cookie fallback and a second Chrome-window
account, against the FIXTURE LinkedIn on a live server. The default account keeps its own card and login."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e._flows import assert_server_logs_clean, connect_linkedin, settled
from tests.support.live_server import LiveServer

GOOD_COOKIE = "AQEDARFIXTURECOOKIE" + "g" * 40
BAD_COOKIE = "invalid-" + "b" * 40


def _row(page: Page, label: str):  # noqa: ANN202
    return page.locator("#linkedin-account-list li").filter(has=page.locator("strong", has_text=label))


def _paste(page: Page, label: str, cookie: str) -> None:
    if page.locator("#paste-cookie").get_attribute("open") is None:  # a swapped-in card starts closed
        page.locator("#paste-cookie summary").click()
    page.fill("#cookie-label", label)
    page.fill("#cookie-value", cookie)
    page.locator("#save-cookie").click()


def test_paste_cookie_validates_good_and_rejects_bad_and_a_second_chrome_account_connects(
    server: LiveServer, ui: Page
) -> None:
    base = server.base_url
    ui.goto(f"{base}/setup")
    # Single default account: the old card is unchanged and the new list shows one row.
    expect(ui.locator("#linkedin-card")).to_have_attribute("data-status", "not_connected")
    expect(ui.locator("#linkedin-account-list li")).to_have_count(1)
    expect(ui.locator("#linkedin-accounts")).to_contain_text("Paste li_at cookie")

    _paste(ui, "Server", GOOD_COOKIE)
    expect(_row(ui, "Server")).to_have_attribute("data-status", "connected", timeout=60_000)
    settled(ui)
    expect(_row(ui, "Server")).to_contain_text("pasted cookie")
    assert GOOD_COOKIE not in ui.content()

    _paste(ui, "Bad", BAD_COOKIE)
    expect(_row(ui, "Bad")).to_have_attribute("data-status", "not_connected", timeout=60_000)
    settled(ui)

    # The default account was never touched by either.
    expect(ui.locator("#linkedin-card")).to_have_attribute("data-status", "not_connected")
    assert server.get_json("/api/session")["stored_status"] == "not_connected"

    # A second Chrome-window account has its own profile and Connect button.
    ui.fill("#new-account-label", "Second")
    ui.get_by_role("button", name="Add account").click()
    expect(_row(ui, "Second")).to_have_count(1)
    ui.get_by_role("button", name="Connect Second").click()
    expect(_row(ui, "Second")).to_have_attribute("data-status", "connected", timeout=90_000)
    settled(ui)
    expect(ui.locator("#linkedin-card")).to_have_attribute("data-status", "not_connected")

    # Disable / enable; the state survives a reload.
    _row(ui, "Second").get_by_role("button", name="Disable").click()
    expect(_row(ui, "Second")).to_have_attribute("data-status", "disabled")
    ui.reload()
    expect(_row(ui, "Second")).to_have_attribute("data-status", "disabled")
    _row(ui, "Second").get_by_role("button", name="Enable").click()
    expect(_row(ui, "Second")).to_have_attribute("data-status", "connected")

    # The default account still connects on its own.
    connect_linkedin(ui, base)
    expect(_row(ui, "Default")).to_have_attribute("data-status", "connected")
    assert_server_logs_clean(server)
    for secret in (GOOD_COOKIE, BAD_COOKIE):
        assert secret not in server.log_text(), "a pasted cookie leaked into the server log"

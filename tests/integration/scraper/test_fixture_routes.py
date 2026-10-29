"""Fixture LinkedIn routes: in-process (TestClient) and in a real headless browser.

Covers the Phase 1.1 2FA login flow (login → 2FA code → remember-this-browser → feed),
the ``/login?direct=1`` shortcut, ``logout-test``, the text-only no-results marker, and the
live-mode 404 guarantee. Phase 1.2 (S4): SDUI results pages, the "…" control menu's
"Copy link to post" variants (urn / noop / none) driving ``navigator.clipboard.writeText``,
the menu click counters, ``legacy-urn-test``, ``permalink-miss-test`` and lazy SDUI
listitems. Never touches real LinkedIn.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, sync_playwright

from sales0.config import Settings
from sales0.linkedin_fixture import build_app
from sales0.linkedin_fixture.corpus import (
    COPY_LINK_QUERY,
    LEGACY_KEYWORD,
    PERMALINK_MISS_KEYWORD,
    SEE_MORE_AT,
    load_corpus,
    permalink_miss_posts,
    posts_for,
)
from sales0.scraper.urls import FIXTURE_PREFIX

P = FIXTURE_PREFIX
SEARCH = f"{P}/search/results/content/"


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), follow_redirects=False)


def _assert_session_cookie(set_cookie: str) -> None:
    assert "li_at=fixture-session" in set_cookie
    lowered = set_cookie.lower()
    assert "httponly" in lowered and "path=/" in lowered and "max-age=31536000" in lowered


def _login_direct(client: TestClient) -> None:
    response = client.get(f"{P}/login", params={"direct": "1"})
    assert response.status_code == 302
    assert response.headers["location"].endswith(f"{P}/feed/")
    _assert_session_cookie(response.headers["set-cookie"])


# ----------------------------------------------------------------- login / 2FA flow
def test_login_without_a_session_renders_the_form_and_sets_no_cookie(client: TestClient) -> None:
    response = client.get(f"{P}/login")
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert 'id="username"' in response.text and 'login-submit' in response.text
    assert f"{P}/checkpoint/challenge/verify" in response.text  # the "user submitted" step


def test_login_with_a_session_redirects_to_the_feed(client: TestClient) -> None:
    _login_direct(client)
    again = client.get(f"{P}/login")
    assert again.status_code == 302 and again.headers["location"].endswith(f"{P}/feed/")
    assert "set-cookie" not in again.headers


def test_2fa_steps_set_the_cookie_only_at_verify_submit(client: TestClient) -> None:
    verify = client.get(f"{P}/checkpoint/challenge/verify")
    assert verify.status_code == 200 and "set-cookie" not in verify.headers
    assert f"{P}/checkpoint/challenge/verify-submit" in verify.text
    assert client.get(f"{P}/feed/").status_code == 302  # still logged out

    submit = client.get(f"{P}/checkpoint/challenge/verify-submit")
    assert submit.status_code == 302
    assert submit.headers["location"].endswith(f"{P}/checkpoint/challenge/remember-browser")
    _assert_session_cookie(submit.headers["set-cookie"])

    remember = client.get(f"{P}/checkpoint/challenge/remember-browser")
    assert remember.status_code == 200 and "Remember this browser" in remember.text
    assert f"{P}/feed/" in remember.text
    assert client.get(f"{P}/feed/").status_code == 200


def test_login_with_an_empty_session_cookie_is_still_logged_out(client: TestClient) -> None:
    client.cookies.set("li_at", "")
    assert client.get(f"{P}/login").status_code == 200
    assert client.get(f"{P}/feed/").status_code == 302


# ----------------------------------------------------------------- feed / authwall
def test_feed_requires_the_session_cookie(client: TestClient) -> None:
    response = client.get(f"{P}/feed/")
    assert response.status_code == 302 and response.headers["location"].endswith(f"{P}/authwall")
    wall = client.get(f"{P}/authwall")
    assert wall.status_code == 200 and "global-nav" not in wall.text and "authwall" in wall.text
    _login_direct(client)
    feed = client.get(f"{P}/feed/")
    assert feed.status_code == 200
    assert 'aria-label="Primary"' in feed.text and "<main" in feed.text and "global-nav" not in feed.text


# ----------------------------------------------------------------- search
def test_search_renders_matching_posts_in_corpus_order_as_sdui_without_urns(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": " AI Workshop ", "datePosted": '"past-week"'})
    assert response.status_code == 200
    keys = [p.key_for("ai workshop") for p in posts_for("ai workshop")]
    order = [response.text.index(f'componentkey="{k}"') for k in keys]
    assert order == sorted(order) and len(keys) == 4
    assert response.text.count('data-view-name="feed-full-update"') == 4
    assert "urn:li:" not in response.text and "data-urn" not in response.text and "data-id" not in response.text
    assert "No results found" not in response.text


def test_legacy_urn_test_renders_the_whole_corpus_in_urn_shapes(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": " Legacy-URN-Test "})
    assert response.status_code == 200
    order = [response.text.index(p.urn.rsplit(":", 1)[1]) for p in load_corpus()]
    assert order == sorted(order)
    assert 'data-urn="urn:li:activity:7300000000000000001"' in response.text  # P1: data-urn shape
    assert 'data-id="urn:li:activity:7300000000000000005"' in response.text  # P5: data-id shape
    assert "feed-full-update" not in response.text and "componentkey" not in response.text


def test_permalink_miss_test_renders_four_noop_menus(client: TestClient) -> None:
    _login_direct(client)
    html = client.get(SEARCH, params={"keywords": PERMALINK_MISS_KEYWORD}).text
    assert html.count('<div data-view-name="feed-control-menu"') == 4
    assert html.count('data-view-name="feed-full-update"') == 4
    order = [html.index(f'componentkey="{p.key_for(PERMALINK_MISS_KEYWORD)}"') for p in permalink_miss_posts()]
    assert order == sorted(order)
    assert "AbCd" not in html  # no share link to copy at all (P2 is forced to noop too)


def test_search_with_zero_matches_renders_the_text_marker_only(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": "quantum basket weaving"})
    assert response.status_code == 200
    assert "No results found" in response.text
    assert "search-reusable" not in response.text
    assert "data-urn" not in response.text and "data-id" not in response.text
    assert 'role="listitem"' not in response.text  # not a drift page either


def test_search_escapes_the_keyword(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in response.text


def test_search_without_cookie_redirects_to_the_authwall(client: TestClient) -> None:
    response = client.get(SEARCH, params={"keywords": "ai workshop"})
    assert response.status_code == 302 and response.headers["location"].endswith(f"{P}/authwall")


def test_logout_test_redirects_to_the_authwall_while_the_cookie_is_still_present(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": " Logout-Test "})
    assert response.status_code == 302 and response.headers["location"].endswith(f"{P}/authwall")
    assert "set-cookie" not in response.headers  # the cookie is NOT cleared: only the path says logout
    assert client.get(f"{P}/feed/").status_code == 200


def test_safety_trigger_keywords_produce_their_pages(client: TestClient) -> None:
    _login_direct(client)
    checkpoint = client.get(SEARCH, params={"keywords": " Checkpoint-Test"})
    assert checkpoint.status_code == 302 and checkpoint.headers["location"].endswith(f"{P}/checkpoint/challenge/")
    assert "security check" in client.get(f"{P}/checkpoint/challenge/").text
    captcha = client.get(SEARCH, params={"keywords": "captcha-test"})
    assert captcha.status_code == 200 and 'src="' in captcha.text and "captcha" in captcha.text
    assert client.get(SEARCH, params={"keywords": "ratelimit-test"}).status_code == 429
    drift = client.get(SEARCH, params={"keywords": "layout-drift-test"})
    assert drift.status_code == 200 and "data-urn" not in drift.text and "No results" not in drift.text
    assert drift.text.count('FeedType_FLAGSHIP_SEARCH"') == 3 and "feed-full-update" not in drift.text
    assert client.get(f"{P}/voyager/api/search/dash/clusters").status_code == 429


def test_lazy_plans_serve_the_first_card_and_template_the_rest(client: TestClient) -> None:
    _login_direct(client)
    lazy = client.get(SEARCH, params={"keywords": "lazy-scroll-test"}).text
    assert lazy.count("<template") == 3  # ai workshop has 4 posts; 1 served up front
    assert lazy.count('data-view-name="feed-full-update"') == 4  # 1 live + 3 templated, all SDUI
    assert client.get(SEARCH, params={"keywords": "checkpoint-scroll-test", "page": "2"}).status_code == 302
    assert "voyager/api/search/dash/clusters" in client.get(SEARCH, params={"keywords": "ratelimit-scroll-test"}).text


def test_post_permalink_serves_the_post_or_404(client: TestClient) -> None:
    _login_direct(client)
    ok = client.get(f"{P}/feed/update/urn:li:activity:7300000000000000008/")
    assert ok.status_code == 200 and "Meridian Insurance" in ok.text
    assert client.get(f"{P}/feed/update/urn:li:activity:1/").status_code == 404


def test_fixture_routes_are_not_mounted_in_live_mode() -> None:
    from sales0.web.app import create_app

    live = TestClient(create_app(Settings(linkedin_mode="live")), follow_redirects=False)
    assert live.get(f"{P}/feed/").status_code == 404
    assert live.get(f"{P}/login").status_code == 404
    assert live.get(f"{P}/checkpoint/challenge/verify-submit").status_code == 404
    fixture = TestClient(create_app(Settings(linkedin_mode="fixture")), follow_redirects=False)
    assert fixture.get(f"{P}/feed/").status_code == 302


# ----------------------------------------------------------------- real browser
@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            yield b
        finally:
            b.close()


@pytest.mark.browser
def test_the_login_pages_walk_through_2fa_and_remember_browser_to_the_feed(
    browser: Browser, fixture_linkedin_server: int
) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    context = browser.new_context()
    try:
        page = context.new_page()
        visited: list[str] = []
        page.on("framenavigated", lambda f: visited.append(f.url) if f == page.main_frame else None)
        page.goto(f"{base}/login")
        page.wait_for_url(f"{base}/feed/", timeout=10_000)
        paths = [u.removeprefix(base).split("?")[0] for u in visited]
        assert paths == [
            "/login",
            "/checkpoint/challenge/verify",
            "/checkpoint/challenge/remember-browser",  # verify-submit 302s straight here
            "/feed/",
        ]
        cookies = {c["name"]: c for c in context.cookies(base)}
        assert cookies["li_at"]["value"] == "fixture-session" and cookies["li_at"]["httpOnly"]
        assert cookies["li_at"]["expires"] > 0  # persistent, survives a browser restart
        assert page.locator("main").count() == 1 and page.locator('nav[aria-label="Primary"]').count() == 1
    finally:
        context.close()


@pytest.mark.browser
def test_the_cookie_is_absent_until_the_2fa_code_is_submitted(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    context = browser.new_context()
    try:
        page = context.new_page()
        # A 204 cancels the navigation, so the page stays frozen on the 2FA step and no cookie is set.
        page.route("**/checkpoint/challenge/verify-submit", lambda route: route.fulfill(status=204))
        page.goto(f"{base}/login")
        page.wait_for_url(f"{base}/checkpoint/challenge/verify", timeout=10_000)
        page.wait_for_timeout(900)  # past the page's own 600 ms auto-submit
        assert page.url == f"{base}/checkpoint/challenge/verify"
        assert not [c for c in context.cookies(base) if c["name"] == "li_at"]
    finally:
        context.close()


@pytest.mark.browser
def test_legacy_see_more_reveals_the_hidden_tail_in_a_browser(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    p1 = next(p for p in load_corpus() if p.id == "P1")
    assert len(p1.text) > SEE_MORE_AT
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(f"{base}/login?direct=1")
        assert page.url == f"{base}/feed/"
        page.goto(f"{base}/search/results/content/?keywords={LEGACY_KEYWORD}")
        card = page.locator(f'[data-urn="{p1.urn}"]')
        text = card.locator('[dir="ltr"]')
        assert "training@acmelogistics.example" not in text.inner_text()
        card.locator('button[aria-label*="see more" i]').click()
        assert "training@acmelogistics.example" in text.inner_text()
        assert card.locator('button[aria-label*="see more" i]').count() == 0
        # The no-results page in the browser: text marker only.
        page.goto(f"{base}/search/results/content/?keywords=zzqx")
        assert page.get_by_role("heading", name="No results found").count() == 1
    finally:
        context.close()


# ----------------------------------------------------------------- Phase 1.2: SDUI in a real browser
# A test-side stand-in for the scraper's clipboard hook: records writeText calls and never
# touches the system clipboard (the page sees it as navigator.clipboard).
CLIPBOARD_SPY_JS = """
(() => {
  window.__spy_copied = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: (t) => { window.__spy_copied.push(String(t)); return Promise.resolve(); },
      write: () => Promise.resolve(),
    },
  });
})();
"""


def _logged_in_page(browser: Browser, base: str, *, spy: bool = True):
    context = browser.new_context()
    if spy:
        context.add_init_script(CLIPBOARD_SPY_JS)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(f"{base}/login?direct=1")
    assert page.url == f"{base}/feed/"
    return context, page, errors


def _post(page, post, keyword):
    return page.locator(f'[componentkey="{post.key_for(keyword)}"] [data-view-name="feed-full-update"]')


def _copy_item(post_locator):
    return post_locator.locator('[data-view-name="feed-control-menu"] [role="menuitem"]', has_text="Copy link to post")


@pytest.mark.browser
def test_sdui_more_button_reveals_the_tail_in_a_browser(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    p1 = next(p for p in load_corpus() if p.id == "P1")
    context, page, errors = _logged_in_page(browser, base)
    try:
        page.goto(f"{base}/search/results/content/?keywords=corporate%20ai%20training")
        post = _post(page, p1, "corporate ai training")
        box = post.locator('[data-testid="expandable-text-box"]')
        assert "training@acmelogistics.example" not in box.inner_text()
        post.locator('[data-testid="expandable-text-button"]').click()
        assert "training@acmelogistics.example" in box.inner_text()
        assert " ".join(box.inner_text().split()) == " ".join(p1.text.split())
        assert post.locator('[data-testid="expandable-text-button"]').count() == 0
        # The second actor's innerText lines are what the SDUI parser reads.
        lines = [ln.strip() for ln in post.locator('[data-view-name="feed-actor"]').nth(1).inner_text().splitlines()]
        assert [ln for ln in lines if ln] == [p1.author_name, "• 3rd+", p1.author_headline, "1d •", "Follow"]
        assert errors == []
    finally:
        context.close()


@pytest.mark.browser
def test_copy_link_urn_variant_writes_the_share_link_through_the_clipboard(
    browser: Browser, fixture_linkedin_server: int
) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    p5 = next(p for p in load_corpus() if p.id == "P5")
    context, page, errors = _logged_in_page(browser, base)
    try:
        page.goto(f"{base}/search/results/content/?keywords=ai%20workshop")
        post = _post(page, p5, "ai workshop")
        trigger = post.locator('[data-view-name="feed-control-menu"] > button')
        assert trigger.get_attribute("aria-label") == f"Open control menu for post by {p5.author_name}"
        assert not _copy_item(post).is_visible()
        trigger.click()
        assert trigger.get_attribute("aria-expanded") == "true"
        assert post.locator('[role="menu"]').is_visible()
        _copy_item(post).click()
        copied = page.evaluate("window.__spy_copied")
        assert copied == [f"{base}/posts/{p5.posts_slug}?{COPY_LINK_QUERY}"]
        assert p5.urn.rsplit(":", 1)[1] in copied[0]  # the URN is recoverable from the copied slug
        assert not post.locator('[role="menu"]').is_visible()  # the menu closes
        assert page.get_by_role("status").filter(has_text="Link copied to clipboard.").count() == 1
        assert page.evaluate("window.__fixture_menu_clicks") == ["Copy link to post"]
        assert page.evaluate("window.__fixture_menu_opens") == 1
        assert errors == []
    finally:
        context.close()


@pytest.mark.browser
def test_copy_link_noop_and_none_variants_write_nothing(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    by_id = {p.id: p for p in load_corpus()}
    context, page, errors = _logged_in_page(browser, base)
    try:
        page.goto(f"{base}/search/results/content/?keywords=ai%20workshop")
        p6 = _post(page, by_id["P6"], "ai workshop")  # noop
        p6.locator('[data-view-name="feed-control-menu"] > button').click()
        _copy_item(p6).click()
        assert page.evaluate("window.__spy_copied") == []
        assert page.get_by_role("status").count() == 0
        assert not p6.locator('[role="menu"]').is_visible()
        p7 = _post(page, by_id["P7"], "ai workshop")  # none: no menu at all
        assert p7.count() == 1 and p7.locator('[data-view-name="feed-control-menu"]').count() == 0
        # Another item, then Escape: recorded, still nothing written; Escape closes an open menu.
        p6.locator('[data-view-name="feed-control-menu"] > button').click()
        p6.locator('[role="menuitem"]', has_text="Save").click()
        p6.locator('[data-view-name="feed-control-menu"] > button').click()
        assert p6.locator('[role="menu"]').is_visible()
        page.keyboard.press("Escape")
        assert not p6.locator('[role="menu"]').is_visible()
        assert page.evaluate("window.__fixture_menu_clicks") == ["Copy link to post", "Save"]
        assert page.evaluate("window.__fixture_menu_opens") == 3
        assert page.evaluate("window.__spy_copied") == []
        assert errors == []
    finally:
        context.close()


@pytest.mark.browser
def test_permalink_miss_test_menus_never_write(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    context, page, errors = _logged_in_page(browser, base)
    try:
        page.goto(f"{base}/search/results/content/?keywords={PERMALINK_MISS_KEYWORD}")
        for post in permalink_miss_posts():  # incl. P2, whose copy_link is normally "urn"
            loc = _post(page, post, PERMALINK_MISS_KEYWORD)
            loc.locator('[data-view-name="feed-control-menu"] > button').click()
            _copy_item(loc).click()
        assert page.evaluate("window.__spy_copied") == []
        assert page.evaluate("window.__fixture_menu_opens") == 4
        assert page.evaluate("window.__fixture_menu_clicks") == ["Copy link to post"] * 4
        assert errors == []
    finally:
        context.close()


@pytest.mark.browser
def test_copy_link_without_any_hook_does_not_break_the_page(browser: Browser, fixture_linkedin_server: int) -> None:
    """With the browser's own clipboard (no hook), the fixture's writeText rejection is swallowed."""
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    p2 = next(p for p in load_corpus() if p.id == "P2")
    context, page, errors = _logged_in_page(browser, base, spy=False)
    try:
        page.goto(f"{base}/search/results/content/?keywords=corporate%20ai%20training")
        post = _post(page, p2, "corporate ai training")
        post.locator('[data-view-name="feed-control-menu"] > button').click()
        _copy_item(post).click()
        page.wait_for_timeout(200)
        assert page.evaluate("window.__fixture_menu_clicks") == ["Copy link to post"]
        assert errors == []
    finally:
        context.close()


@pytest.mark.browser
def test_lazy_scroll_appends_sdui_listitems_to_the_lazy_column(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    context, page, errors = _logged_in_page(browser, base)
    try:
        page.goto(f"{base}/search/results/content/?keywords=lazy-scroll-test")
        column = page.locator('[data-testid="lazy-column"]')
        items = column.locator(':scope > [role="listitem"][componentkey]')
        assert items.count() == 1
        box = column.bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, 600)
        page.wait_for_function(
            """() => document.querySelectorAll('[data-testid="lazy-column"] > [role="listitem"]').length === 2""",
            timeout=5_000,
        )
        revealed = items.nth(1).locator('[data-view-name="feed-full-update"]')
        assert revealed.count() == 1
        p5 = next(p for p in load_corpus() if p.id == "P5")
        assert items.nth(1).get_attribute("componentkey") == f"expanded{p5.key_for('lazy-scroll-test')}FeedType_FLAGSHIP_SEARCH"
        # A revealed post's menu works like any other.
        revealed.locator('[data-view-name="feed-control-menu"] > button').click()
        _copy_item(revealed).click()
        assert page.evaluate("window.__spy_copied") == [f"{base}/posts/{p5.posts_slug}?{COPY_LINK_QUERY}"]
        assert errors == []
    finally:
        context.close()

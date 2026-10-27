"""Fixture LinkedIn routes: in-process (TestClient) and in a real headless browser.

Covers the Phase 1.1 2FA login flow (login → 2FA code → remember-this-browser → feed),
the ``/login?direct=1`` shortcut, ``logout-test``, the text-only no-results marker, and the
live-mode 404 guarantee. Never touches real LinkedIn.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, sync_playwright

from sales0.config import Settings
from sales0.linkedin_fixture import build_app
from sales0.linkedin_fixture.corpus import SEE_MORE_AT, load_corpus
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
def test_search_renders_matching_posts_in_corpus_order(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": " AI Workshop ", "datePosted": '"past-week"'})
    assert response.status_code == 200
    order = [response.text.index(f"73000000000000000{n}") for n in ("01", "05", "06", "07")]
    assert order == sorted(order)
    assert 'data-urn="urn:li:activity:7300000000000000001"' in response.text  # P1: data-urn shape
    assert 'data-id="urn:li:activity:7300000000000000005"' in response.text  # P5: data-id shape
    assert "No results found" not in response.text


def test_search_with_zero_matches_renders_the_text_marker_only(client: TestClient) -> None:
    _login_direct(client)
    response = client.get(SEARCH, params={"keywords": "quantum basket weaving"})
    assert response.status_code == 200
    assert "No results found" in response.text
    assert "search-reusable" not in response.text
    assert "data-urn" not in response.text and "data-id" not in response.text


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
    assert client.get(f"{P}/voyager/api/search/dash/clusters").status_code == 429


def test_lazy_plans_serve_the_first_card_and_template_the_rest(client: TestClient) -> None:
    _login_direct(client)
    lazy = client.get(SEARCH, params={"keywords": "lazy-scroll-test"}).text
    assert lazy.count("<template") == 3  # ai workshop has 4 posts; 1 served up front
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
def test_see_more_reveals_the_hidden_tail_in_a_browser(browser: Browser, fixture_linkedin_server: int) -> None:
    base = f"http://127.0.0.1:{fixture_linkedin_server}{P}"
    p1 = next(p for p in load_corpus() if p.id == "P1")
    assert len(p1.text) > SEE_MORE_AT
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(f"{base}/login?direct=1")
        assert page.url == f"{base}/feed/"
        page.goto(f"{base}/search/results/content/?keywords=corporate%20ai%20training")
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

"""Connect + session check on a real headless browser against the fixture LinkedIn.

Connect walks the fixture's login -> 2FA -> remember-browser -> feed flow and must succeed
only once the feed is reached (cookie + URL rule), never on a DOM marker. The default
channel here is the bundled Chromium (tests/conftest.py); real Chrome is test_real_chrome.py.
"""

from __future__ import annotations

import time

import pytest

from sales0.config import Settings
from sales0.scraper import connect as connect_module
from sales0.scraper.browser import first_page, open_context, profile_dir
from sales0.scraper.connect import (
    RATE_LIMITED_MESSAGE,
    TIMEOUT_MESSAGE,
    WINDOW_CLOSED_MESSAGE,
    connect_interactive,
)
from sales0.scraper.detection import classify_url
from sales0.scraper.errors import BrowserUnavailable
from sales0.scraper.session_check import CheckDetails, check_session, verify_in_context
from sales0.scraper.types import SessionCheckResult
from sales0.scraper.urls import base_url
from tests.integration.scraper.conftest import log_in_profile

pytestmark = pytest.mark.browser

NEVER = lambda: False  # noqa: E731


def _record_verifications(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Wrap Connect's verification step: record (page URL at call time, monotonic time)."""
    calls: list[tuple[str, float]] = []
    original = connect_module.verify_with_details

    def wrapped(context, base, timeout_ms, page=None):  # type: ignore[no-untyped-def]
        calls.append((page.url if page is not None else "", time.monotonic()))
        return original(context, base, timeout_ms, page=page)

    monkeypatch.setattr(connect_module, "verify_with_details", wrapped)
    return calls


# ------------------------------------------------------------------------------ session check
def test_a_fresh_profile_is_not_logged_in_and_the_check_never_navigates(fixture_settings: Settings) -> None:
    base = base_url(fixture_settings)
    with open_context(fixture_settings, headless=True) as context:
        requests: list[str] = []
        context.on("request", lambda req: requests.append(req.url))
        result = verify_in_context(context, base, 2_000)
    assert (result.logged_in, result.reason) == (False, "no_cookie")
    assert result.url == f"{base}/feed/"
    assert [u for u in requests if u.startswith(base)] == []

    via_check = check_session(fixture_settings)
    assert (via_check.logged_in, via_check.reason) == (False, "no_cookie")


def test_a_logged_in_profile_checks_ok_headless_with_soft_hints_but_no_dom_requirement(
    connected_settings: Settings,
) -> None:
    result = check_session(connected_settings)
    assert (result.logged_in, result.reason) == (True, "ok")
    assert result.url == f"{base_url(connected_settings)}/feed/"
    assert result.soft_hints  # the fixture feed carries main / nav[aria-label] / an /in/ link
    assert "legacy_global_nav" not in result.soft_hints


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (429, "<main>slow down</main>", "rate_limited"),
        (999, "<main>denied</main>", "rate_limited"),
        (500, "<main>oops</main>", "error"),
        (200, '<main><form action="/login-submit"><input id="username"></form></main>', "redirected_login"),
        (200, '<main><iframe src="https://client-api.arkoselabs.com/x"></iframe></main>', "captcha"),
    ],
)
def test_verify_maps_page_verdicts_to_session_reasons(
    connected_settings: Settings, status: int, body: str, reason: str
) -> None:
    base = base_url(connected_settings)
    with open_context(connected_settings, headless=True) as context:
        context.route(
            f"{base}/feed/",
            lambda route: route.fulfill(status=status, body=body, content_type="text/html"),
        )
        result = verify_in_context(context, base, 1_000)
    assert (result.logged_in, result.reason) == (False, reason)


def test_verify_reports_a_checkpoint_when_the_feed_redirects_to_a_challenge(connected_settings: Settings) -> None:
    base = base_url(connected_settings)
    with open_context(connected_settings, headless=True) as context:
        context.route(
            f"{base}/feed/",
            lambda route: route.fulfill(status=302, headers={"Location": f"{base}/checkpoint/challenge/"}),
        )
        result = verify_in_context(context, base, 1_000)
    assert (result.logged_in, result.reason) == (False, "checkpoint")
    assert "/checkpoint/challenge/" in result.url


def test_verify_uses_the_given_page(connected_settings: Settings) -> None:
    base = base_url(connected_settings)
    with open_context(connected_settings, headless=True) as context:
        other = context.new_page()
        result = verify_in_context(context, base, 2_000, page=other)
        assert result.logged_in is True
        assert other.url == f"{base}/feed/"
        assert first_page(context).url == "about:blank"


def test_check_session_propagates_browser_unavailable_for_the_job_to_show(
    fixture_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", "chrome")
    from sales0.config import get_settings, reset_settings
    from sales0.scraper import browser

    reset_settings()
    browser.reset_browser_cache()
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: None)
    with pytest.raises(BrowserUnavailable, match="Google Chrome is not installed"):
        check_session(get_settings())


# ------------------------------------------------------------------------------ connect
def test_connect_walks_login_2fa_remember_browser_and_succeeds_only_on_the_feed(
    fixture_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_verifications(monkeypatch)
    ticks: list[int] = []
    started = time.monotonic()
    result = connect_interactive(fixture_settings, on_tick=ticks.append, should_cancel=NEVER, poll_interval_s=0.2)
    finished = time.monotonic()

    assert (result.connected, result.reason, result.detail, result.browser) == (True, "ok", None, "chromium")
    # Never verified while on /login or /checkpoint/... (2FA, remember-browser), only after the feed.
    assert calls, "connect never verified a page"
    for url, _at in calls:
        assert classify_url(url) is None, url
    assert calls[0][0] == f"{base_url(fixture_settings)}/feed/"
    # login 300 ms + 2FA 600 ms + remember-browser 600 ms precede the feed.
    assert finished - started >= 1.4
    assert finished - calls[0][1] < 3.0  # success within 3 s of reaching the feed
    assert len(ticks) >= 5 and ticks == sorted(ticks)

    profile = profile_dir(fixture_settings, "chromium")
    assert profile.name == "linkedin-profile-fixture-chromium" and any(profile.iterdir())
    # A brand-new headless browser process on the same profile is still logged in.
    check = check_session(fixture_settings)
    assert (check.logged_in, check.reason) == (True, "ok")


def test_connect_keeps_waiting_on_a_checkpoint_page_even_with_the_cookie_set(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_verifications(monkeypatch)
    result = connect_interactive(
        connected_settings,
        on_tick=lambda _s: None,
        should_cancel=NEVER,
        poll_interval_s=0.1,
        timeout_s=1.2,
        start_url=f"{base_url(connected_settings)}/checkpoint/challenge/",
    )
    assert (result.connected, result.reason, result.detail) == (False, "timeout", TIMEOUT_MESSAGE)
    assert calls == []


def test_connect_on_an_already_logged_in_profile_succeeds_on_the_first_poll(connected_settings: Settings) -> None:
    ticks: list[int] = []
    result = connect_interactive(connected_settings, on_tick=ticks.append, should_cancel=NEVER, poll_interval_s=0.2)
    assert (result.connected, result.reason) == (True, "ok")
    assert len(ticks) == 1


def test_connect_does_not_reverify_a_page_that_failed_until_its_url_changes(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fails(context, base, timeout_ms, page=None):  # type: ignore[no-untyped-def]
        attempts.append(page.url)
        return (
            SessionCheckResult(logged_in=False, reason="redirected_login", url=page.url),
            CheckDetails(True, "/feed/", 200, "logout:login_form"),
        )

    monkeypatch.setattr(connect_module, "verify_with_details", fails)
    result = connect_interactive(
        connected_settings, on_tick=lambda _s: None, should_cancel=NEVER, poll_interval_s=0.1, timeout_s=1.0
    )
    assert result.reason == "timeout"
    assert len(attempts) == 1


def test_connect_stops_with_a_message_when_linkedin_rate_limits_the_verification(
    connected_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def limited(context, base, timeout_ms, page=None):  # type: ignore[no-untyped-def]
        return SessionCheckResult(False, "rate_limited", page.url), CheckDetails(True, "/feed/", 429, "rate_limited")

    monkeypatch.setattr(connect_module, "verify_with_details", limited)
    result = connect_interactive(connected_settings, on_tick=lambda _s: None, should_cancel=NEVER, poll_interval_s=0.1)
    assert (result.connected, result.reason, result.detail) == (False, "error", RATE_LIMITED_MESSAGE)
    assert RATE_LIMITED_MESSAGE == "LinkedIn is rate-limiting this browser — wait a few hours, then Connect again"


def test_connect_times_out_when_the_user_never_logs_in(fixture_settings: Settings) -> None:
    ticks: list[int] = []
    result = connect_interactive(
        fixture_settings,
        on_tick=ticks.append,
        should_cancel=NEVER,
        poll_interval_s=0.2,
        timeout_s=1.2,
        start_url=f"{base_url(fixture_settings)}/authwall",
    )
    assert (result.connected, result.reason, result.detail) == (False, "timeout", TIMEOUT_MESSAGE)
    assert len(ticks) >= 3 and ticks == sorted(ticks)
    assert check_session(fixture_settings).reason == "no_cookie"


def test_connect_can_be_cancelled_by_the_user(fixture_settings: Settings) -> None:
    ticks: list[int] = []
    result = connect_interactive(
        fixture_settings,
        on_tick=ticks.append,
        should_cancel=lambda: len(ticks) >= 2,
        poll_interval_s=0.1,
        start_url=f"{base_url(fixture_settings)}/authwall",
    )
    assert (result.connected, result.reason) == (False, "cancelled")
    assert len(ticks) == 2


def test_connect_reports_a_closed_window(fixture_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = []
    original = connect_module.first_page

    def capture(context):  # type: ignore[no-untyped-def]
        page = original(context)
        opened.append(page)
        return page

    monkeypatch.setattr(connect_module, "first_page", capture)
    result = connect_interactive(
        fixture_settings,
        on_tick=lambda _s: opened[0].close(),
        should_cancel=NEVER,
        poll_interval_s=0.1,
        timeout_s=10,
        start_url=f"{base_url(fixture_settings)}/authwall",
    )
    assert (result.connected, result.reason, result.detail) == (False, "window_closed", WINDOW_CLOSED_MESSAGE)


def test_connect_reports_browser_unavailable_as_an_error_with_its_message(
    fixture_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales0.scraper import browser

    def missing(_settings):  # type: ignore[no-untyped-def]
        raise BrowserUnavailable(browser.CHROME_MISSING_MESSAGE)

    monkeypatch.setattr(connect_module, "resolve_browser", missing)
    result = connect_interactive(fixture_settings, on_tick=lambda _s: None, should_cancel=NEVER)
    assert (result.connected, result.reason, result.detail) == (False, "error", browser.CHROME_MISSING_MESSAGE)


def test_connect_reports_a_profile_still_held_by_an_open_sales0_window(fixture_settings: Settings) -> None:
    import os
    import socket

    profile = profile_dir(fixture_settings, "chromium")
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    result = connect_interactive(fixture_settings, on_tick=lambda _s: None, should_cancel=NEVER)
    assert (result.connected, result.reason) == (False, "error")
    assert result.detail == "The sales0 browser window is still open — close it and try again"


def test_the_session_survives_a_restart_after_logging_in(fixture_settings: Settings) -> None:
    log_in_profile(fixture_settings)
    first = check_session(fixture_settings)
    second = check_session(fixture_settings)
    assert (first.reason, second.reason) == ("ok", "ok")

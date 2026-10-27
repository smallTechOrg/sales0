"""Connect + session check on a real headless Chromium against the fixture server."""

from __future__ import annotations

import pytest

from sales0.config import Settings
from sales0.scraper import connect as connect_module
from sales0.scraper.browser import profile_dir
from sales0.scraper.connect import TIMEOUT_MESSAGE, connect_interactive
from sales0.scraper.session_check import check_session
from sales0.scraper.urls import base_url

pytestmark = pytest.mark.browser


def test_fresh_profile_is_not_logged_in(fixture_settings: Settings) -> None:
    result = check_session(fixture_settings)
    assert result.logged_in is False
    assert result.reason == "no_cookie"
    assert result.url.endswith("/__fixtures/linkedin/feed/")


def test_connect_logs_in_and_the_session_survives_reopening_the_profile(fixture_settings: Settings) -> None:
    ticks: list[int] = []
    result = connect_interactive(fixture_settings, on_tick=ticks.append, should_cancel=lambda: False)
    assert (result.connected, result.reason, result.detail) == (True, "ok", None)
    profile = profile_dir(fixture_settings)
    assert profile.name == "linkedin-profile-fixture" and profile.is_dir() and any(profile.iterdir())
    # A brand-new browser process on the same profile is still logged in (persistent profile).
    check = check_session(fixture_settings, headless=False)  # fixture mode forces headless anyway
    assert (check.logged_in, check.reason) == (True, "ok")
    assert check.url == f"{base_url(fixture_settings)}/feed/"


def test_connect_times_out_when_the_user_never_logs_in(fixture_settings: Settings) -> None:
    ticks: list[int] = []
    result = connect_interactive(
        fixture_settings,
        on_tick=ticks.append,
        should_cancel=lambda: False,
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
        should_cancel=lambda: False,
        poll_interval_s=0.1,
        timeout_s=10,
        start_url=f"{base_url(fixture_settings)}/authwall",
    )
    assert (result.connected, result.reason) == (False, "window_closed")
    assert result.detail and "closed" in result.detail


def test_connect_reports_an_error_when_the_profile_cannot_open(
    fixture_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales0.scraper.errors import ScrapeError

    def boom(_profile, _headless):  # type: ignore[no-untyped-def]
        raise ScrapeError("Could not open the LinkedIn browser profile — locked")

    monkeypatch.setattr(connect_module, "open_context", boom)
    result = connect_interactive(fixture_settings, on_tick=lambda _s: None, should_cancel=lambda: False)
    assert (result.connected, result.reason) == (False, "error")
    assert "locked" in (result.detail or "")

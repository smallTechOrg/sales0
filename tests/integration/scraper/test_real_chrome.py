"""Real Google Chrome, HEADLESS, against the fixture LinkedIn (marker ``chrome``).

The profile lives under this test's tmp data dir (never ``data/``) and only the local fixture
server is contacted. If Google Chrome is not installed the test FAILS (it is part of the
Phase 1.1 gate on the user's Mac), it is never skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sales0.config import REPO_ROOT, Settings, get_settings, reset_settings
from sales0.linkedin_fixture.corpus import posts_for
from sales0.scraper import browser
from sales0.scraper.browser import find_chrome, first_page, open_context, profile_dir, system_locale
from sales0.scraper.connect import connect_interactive
from sales0.scraper.search import LinkedInSearcher
from sales0.scraper.session_check import check_session
from sales0.scraper.urls import base_url
from tests.integration.scraper.conftest import log_in_profile

pytestmark = [pytest.mark.browser, pytest.mark.chrome]

NOT_INSTALLED = "Google Chrome not installed — install it, or run this gate on the user's Mac"


@pytest.fixture
def chrome_settings(fixture_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    if find_chrome() is None:
        pytest.fail(NOT_INSTALLED)
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", "chrome")
    reset_settings()
    browser.reset_browser_cache()
    settings = get_settings()
    assert settings.linkedin_browser_channel == "chrome"
    assert not settings.data_path.is_relative_to(REPO_ROOT / "data")  # never the user's profiles
    return settings


# Chrome's ICU reports some zones by their legacy IANA link name (Asia/Kolkata ->
# Asia/Calcutta). Same zone, so compare the rules, not the spelling.
_PROBE_INSTANTS = [datetime(year, month, 15, 12, tzinfo=UTC) for year in (1945, 1970, 1990, 2010, 2026) for month in (1, 7)]


def _assert_same_zone(reported: str, configured: str) -> None:
    if reported == configured:
        return
    got, want = ZoneInfo(reported), ZoneInfo(configured)
    for instant in _PROBE_INSTANTS:
        assert instant.astimezone(got).utcoffset() == instant.astimezone(want).utcoffset(), (reported, configured, instant)


def _installed_major() -> str:
    install = find_chrome()
    assert install is not None, NOT_INSTALLED
    version = browser.chrome_version(install)
    assert version, "could not read the installed Chrome version"
    return version.split(".")[0]


def test_headless_chrome_looks_like_a_normal_chrome(chrome_settings: Settings) -> None:
    choice = browser.resolve_browser(chrome_settings)
    assert choice.channel == "chrome" and choice.label == f"Google Chrome {_installed_major()}"
    base = base_url(chrome_settings)
    with open_context(chrome_settings, headless=True) as context:
        page = first_page(context)
        page.goto(f"{base}/authwall", wait_until="domcontentloaded")
        facts = page.evaluate(
            """() => ({
                webdriver: navigator.webdriver,
                ua: navigator.userAgent,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                lang: navigator.language,
            })"""
        )
        cdp = context.new_cdp_session(page)
        product = cdp.send("Browser.getVersion")["product"]
    assert facts["webdriver"] is False
    assert "HeadlessChrome" not in facts["ua"]
    assert f"Chrome/{_installed_major()}." in facts["ua"]
    _assert_same_zone(facts["tz"], chrome_settings.timezone)
    assert facts["lang"] == system_locale()
    assert product.split("/")[-1].split(".")[0] == _installed_major()  # really Google Chrome's build
    assert profile_dir(chrome_settings, "chrome").is_relative_to(chrome_settings.data_path)


def test_fixture_2fa_login_is_detected_in_chrome_by_cookie_and_url(chrome_settings: Settings) -> None:
    ticks: list[int] = []
    result = connect_interactive(chrome_settings, on_tick=ticks.append, should_cancel=lambda: False, poll_interval_s=0.25)
    assert (result.connected, result.reason, result.browser) == (True, "ok", "chrome")
    profile = profile_dir(chrome_settings, "chrome")
    assert profile.name == "linkedin-profile-fixture-chrome" and any(profile.iterdir())
    check = check_session(chrome_settings)  # a new headless Chrome process on the same profile
    assert (check.logged_in, check.reason) == (True, "ok")


def test_a_full_search_in_chrome_returns_the_four_ai_workshop_posts(chrome_settings: Settings) -> None:
    log_in_profile(chrome_settings)
    expected = [p.urn for p in posts_for("ai workshop")]
    assert len(expected) == 4
    with LinkedInSearcher(chrome_settings) as searcher:
        result = searcher.search("ai workshop", 15)
    assert sorted(p.urn for p in result.posts) == sorted(expected)
    assert result.drift_warning is False
    by_urn = {p.urn: p for p in result.posts}
    for post in posts_for("ai workshop"):
        assert by_urn[post.urn].author_name == post.author_name
        assert by_urn[post.urn].text.strip()

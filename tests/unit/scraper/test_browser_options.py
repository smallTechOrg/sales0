"""Profiles, launch options, UA and locale (architecture.md -> LinkedIn browser). Pure."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from sales0.config import Settings
from sales0.scraper import browser
from sales0.scraper.browser import (
    PROFILE_IN_USE_MESSAGE,
    BrowserChoice,
    launch_options,
    normalize_locale,
    open_context,
    profile_dir,
    profile_in_use,
    reset_browser_cache,
    system_locale,
    user_agent_for,
)
from sales0.scraper.errors import BrowserUnavailable

CHROME = BrowserChoice(channel="chrome", version="156.0.8078.48", label="Google Chrome 156", note=None)
CHROMIUM = BrowserChoice(channel="chromium", version="156.0.8078.4", label="Built-in browser (Chromium)", note=None)


@pytest.fixture
def fixed_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_browser_cache()
    monkeypatch.setattr(browser, "system_locale", lambda: "en-IN")


@pytest.fixture
def real_locale():  # type: ignore[no-untyped-def]
    """The real cached ``system_locale``, with its cache cleared before and after."""
    reset_browser_cache()
    yield browser.system_locale
    reset_browser_cache()


# ------------------------------------------------------------------------------ profiles
@pytest.mark.parametrize(
    ("mode", "channel", "dirname"),
    [
        ("live", "chrome", "linkedin-chrome-profile"),
        ("live", "chromium", "linkedin-profile"),
        ("fixture", "chrome", "linkedin-profile-fixture-chrome"),
        ("fixture", "chromium", "linkedin-profile-fixture-chromium"),
    ],
)
def test_profile_dir_is_split_by_mode_and_channel_under_the_data_dir(
    tmp_path: Path, mode: str, channel: str, dirname: str
) -> None:
    settings = Settings(linkedin_mode=mode, data_dir=str(tmp_path))
    assert profile_dir(settings, channel) == tmp_path / dirname


def test_a_chrome_profile_and_a_chromium_profile_are_never_shared(tmp_path: Path) -> None:
    for mode in ("live", "fixture"):
        settings = Settings(linkedin_mode=mode, data_dir=str(tmp_path))
        assert profile_dir(settings, "chrome") != profile_dir(settings, "chromium")


def test_profile_dir_rejects_an_unknown_channel(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown browser channel"):
        profile_dir(Settings(data_dir=str(tmp_path)), "firefox")


def test_the_default_live_chrome_profile_is_inside_the_gitignored_repo_data_dir() -> None:
    assert profile_dir(Settings(linkedin_mode="live", data_dir="data"), "chrome").parts[-2:] == (
        "data",
        "linkedin-chrome-profile",
    )


# ------------------------------------------------------------------------------ launch options
def test_headed_and_headless_launches_share_the_identical_argument_set_except_the_marked_rows(
    tmp_path: Path, fixed_locale: None
) -> None:
    settings = Settings(data_dir=str(tmp_path), timezone="Asia/Kolkata")
    headed = launch_options(settings, CHROME, headless=False)
    headless = launch_options(settings, CHROME, headless=True)

    assert headed == {
        "user_data_dir": str(tmp_path / "linkedin-chrome-profile"),
        "channel": "chrome",
        "headless": False,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,860",
        ],
        "chromium_sandbox": True,
        "no_viewport": True,
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
    }
    assert headless == {
        "user_data_dir": str(tmp_path / "linkedin-chrome-profile"),
        "channel": "chrome",
        "headless": True,
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
        "chromium_sandbox": True,
        "viewport": {"width": 1280, "height": 860},
        "user_agent": user_agent_for("156.0.8078.48"),
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
    }


def test_the_cookie_encryption_defaults_are_never_removed(tmp_path: Path, fixed_locale: None) -> None:
    for headless in (True, False):
        options = launch_options(Settings(data_dir=str(tmp_path)), CHROME, headless=headless)
        assert options["ignore_default_args"] == ["--enable-automation"]
        assert "--use-mock-keychain" not in options["ignore_default_args"]
        assert "--password-store=basic" not in options["ignore_default_args"]


def test_chromium_launches_with_the_chromium_channel_on_its_own_profile(tmp_path: Path, fixed_locale: None) -> None:
    options = launch_options(Settings(data_dir=str(tmp_path)), CHROMIUM, headless=True)
    assert options["channel"] == "chromium" and "executable_path" not in options
    assert options["user_data_dir"] == str(tmp_path / "linkedin-profile")
    assert "Chrome/156.0.0.0" in options["user_agent"]


def test_a_user_applications_chrome_gets_executable_path_instead_of_channel(tmp_path: Path, fixed_locale: None) -> None:
    choice = BrowserChoice(
        channel="chrome", version="156.0.1.2", label="Google Chrome 156", note=None, executable_path="/x/Chrome"
    )
    options = launch_options(Settings(data_dir=str(tmp_path)), choice, headless=False)
    assert options["executable_path"] == "/x/Chrome" and "channel" not in options


def test_an_unknown_version_keeps_the_browsers_own_user_agent(tmp_path: Path, fixed_locale: None) -> None:
    choice = BrowserChoice(channel="chromium", version=None, label="Built-in browser (Chromium)", note=None)
    assert "user_agent" not in launch_options(Settings(data_dir=str(tmp_path)), choice, headless=True)


def test_timezone_follows_sales0_timezone(tmp_path: Path, fixed_locale: None) -> None:
    options = launch_options(Settings(data_dir=str(tmp_path), timezone="Europe/Berlin"), CHROME, headless=True)
    assert options["timezone_id"] == "Europe/Berlin"


# ------------------------------------------------------------------------------ user agent
@pytest.mark.parametrize(
    ("platform", "token"),
    [
        ("darwin", "Macintosh; Intel Mac OS X 10_15_7"),
        ("linux", "X11; Linux x86_64"),
        ("win32", "Windows NT 10.0; Win64; x64"),
    ],
)
def test_user_agent_is_chromes_reduced_ua_and_never_headless(platform: str, token: str) -> None:
    ua = user_agent_for("156.0.8078.48", platform=platform)
    assert ua == f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    assert "HeadlessChrome" not in ua


def test_user_agent_accepts_a_bare_major_version() -> None:
    assert "Chrome/157.0.0.0 " in user_agent_for("157", platform="linux")


# ------------------------------------------------------------------------------ locale
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en_IN", "en-IN"),
        ("en_IN\n", "en-IN"),
        ("en_IN.UTF-8", "en-IN"),
        ("en_US@rg=inzzzz", "en-US"),
        ("de_DE@euro", "de-DE"),
        ("hi", "hi"),
        ("C", None),
        ("POSIX", None),
        ("", None),
        (None, None),
        ("!!", None),
    ],
)
def test_normalize_locale_produces_bcp47(raw: str | None, expected: str | None) -> None:
    assert normalize_locale(raw) == expected


def test_system_locale_reads_apple_locale_first_on_macos(monkeypatch: pytest.MonkeyPatch, real_locale) -> None:  # type: ignore[no-untyped-def]
    fn = real_locale
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        assert kwargs["timeout"] == 2
        return subprocess.CompletedProcess(cmd, 0, stdout="en_IN\n", stderr="")

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    assert fn() == "en-IN"
    assert calls == [["defaults", "read", "-g", "AppleLocale"]]
    fn.cache_clear()


def test_system_locale_falls_back_to_lc_all_then_lang_then_en_us(monkeypatch: pytest.MonkeyPatch, real_locale) -> None:  # type: ignore[no-untyped-def]
    fn = real_locale
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert fn() == "en-GB"
    fn.cache_clear()
    monkeypatch.delenv("LC_ALL")
    assert fn() == "fr-FR"
    fn.cache_clear()
    monkeypatch.setenv("LANG", "C")
    assert fn() == "en-US"
    fn.cache_clear()


def test_system_locale_survives_a_failing_defaults_command(monkeypatch: pytest.MonkeyPatch, real_locale) -> None:  # type: ignore[no-untyped-def]
    fn = real_locale
    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("LANG", "en_IN.UTF-8")

    def boom(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd, 2)

    monkeypatch.setattr(browser.subprocess, "run", boom)
    assert fn() == "en-IN"
    fn.cache_clear()


def test_system_locale_is_cached(monkeypatch: pytest.MonkeyPatch, real_locale) -> None:  # type: ignore[no-untyped-def]
    fn = real_locale
    first = fn()
    assert fn() is first
    assert system_locale is fn
    fn.cache_clear()


# ------------------------------------------------------------------------------ profile lock
def test_a_profile_without_a_lock_is_free(tmp_path: Path) -> None:
    assert profile_in_use(tmp_path) is False


def test_a_lock_held_by_a_live_process_on_this_host_means_in_use(tmp_path: Path) -> None:
    (tmp_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    assert profile_in_use(tmp_path) is True


def test_a_stale_lock_from_a_dead_process_is_left_to_chrome(tmp_path: Path) -> None:
    proc = subprocess.Popen(["true"])
    proc.wait()
    (tmp_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-{proc.pid}")
    assert profile_in_use(tmp_path) is False


def test_a_lock_from_another_host_or_with_junk_is_not_treated_as_in_use(tmp_path: Path) -> None:
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"some-other-host-{os.getpid()}")
    assert profile_in_use(tmp_path) is False
    lock.unlink()
    lock.symlink_to("garbage")
    assert profile_in_use(tmp_path) is False


def test_open_context_refuses_a_profile_held_by_an_open_sales0_window_before_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixed_locale: None
) -> None:
    settings = Settings(data_dir=str(tmp_path), linkedin_browser_channel="chromium")
    monkeypatch.setattr(browser, "resolve_browser", lambda _s: CHROMIUM)

    def must_not_launch(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("must not launch a browser on a locked profile")

    monkeypatch.setattr(browser, "launch_persistent", must_not_launch)
    profile = profile_dir(settings, "chromium")
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    with pytest.raises(BrowserUnavailable) as caught, open_context(settings, headless=True):
        pass
    assert str(caught.value) == PROFILE_IN_USE_MESSAGE == (
        "The sales0 browser window is still open — close it and try again"
    )


def test_open_context_propagates_a_missing_chrome_before_touching_any_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: None)
    settings = Settings(data_dir=str(tmp_path), linkedin_browser_channel="chrome")
    with pytest.raises(BrowserUnavailable, match="Google Chrome is not installed"), open_context(
        settings, headless=True
    ):
        pass
    assert not any(tmp_path.iterdir())


def test_launch_failures_name_the_browser_and_the_next_step() -> None:
    message = browser.launch_failed_message(CHROME, RuntimeError("Executable doesn't exist\nlong log"))
    assert message == (
        "Could not start Google Chrome (Executable doesn't exist) — quit any sales0 Chrome window and try again"
    )

"""Browser resolution (architecture.md -> LinkedIn browser -> Browser resolution). Pure: the
Chrome install is stubbed or faked on disk; no browser is started."""

from __future__ import annotations

import json
import plistlib
import stat
from pathlib import Path

import pytest

from sales0.config import Settings
from sales0.scraper import browser
from sales0.scraper.browser import (
    CHROME_MISSING_MESSAGE,
    FALLBACK_NOTE,
    BrowserChoice,
    ChromeInstall,
    chrome_version,
    chromium_version,
    find_chrome,
    reset_browser_cache,
    resolve_browser,
)
from sales0.scraper.errors import BrowserUnavailable, ScrapeError


def _fake_app(root: Path, version: str = "156.0.8078.48") -> Path:
    app = root / "Google Chrome.app"
    binary = app / "Contents" / "MacOS" / "Google Chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    with (app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": version}, fh)
    return app


@pytest.fixture
def chrome_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ChromeInstall:
    app = _fake_app(tmp_path / "Applications")
    install = ChromeInstall(binary=app / "Contents/MacOS/Google Chrome", app=app)
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: install)
    monkeypatch.setattr(browser, "chromium_version", lambda *a, **k: "156.0.8078.4")
    reset_browser_cache()
    return install


@pytest.fixture
def chrome_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: None)
    monkeypatch.setattr(browser, "chromium_version", lambda *a, **k: "156.0.8078.4")
    reset_browser_cache()


def test_auto_with_chrome_installed_resolves_to_chrome_with_its_major_in_the_label(chrome_installed) -> None:
    choice = resolve_browser(Settings(linkedin_browser_channel="auto"))
    assert choice == BrowserChoice(
        channel="chrome", version="156.0.8078.48", label="Google Chrome 156", note=None, executable_path=None
    )


def test_auto_without_chrome_falls_back_to_chromium_with_the_setup_note(chrome_absent) -> None:
    choice = resolve_browser(Settings(linkedin_browser_channel="auto"))
    assert (choice.channel, choice.label, choice.note) == ("chromium", "Built-in browser (Chromium)", FALLBACK_NOTE)
    assert choice.version == "156.0.8078.4"
    assert FALLBACK_NOTE == "Google Chrome not found — using the built-in browser"


def test_chrome_channel_with_chrome_installed_resolves_to_chrome(chrome_installed) -> None:
    assert resolve_browser(Settings(linkedin_browser_channel="chrome")).channel == "chrome"


def test_chrome_channel_without_chrome_raises_browser_unavailable_with_the_next_step(chrome_absent) -> None:
    with pytest.raises(BrowserUnavailable) as caught:
        resolve_browser(Settings(linkedin_browser_channel="chrome"))
    assert str(caught.value) == CHROME_MISSING_MESSAGE
    assert "https://www.google.com/chrome/" in str(caught.value)
    assert "SALES0_LINKEDIN_BROWSER_CHANNEL=auto" in str(caught.value)
    assert isinstance(caught.value, ScrapeError)


def test_chromium_channel_never_looks_for_chrome_and_has_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("find_chrome must not be called for channel=chromium")

    monkeypatch.setattr(browser, "find_chrome", must_not_run)
    reset_browser_cache()
    choice = resolve_browser(Settings(linkedin_browser_channel="chromium"))
    assert (choice.channel, choice.note, choice.label) == ("chromium", None, "Built-in browser (Chromium)")


def test_resolution_is_cached_until_the_reset_seam(monkeypatch: pytest.MonkeyPatch, chrome_installed) -> None:
    calls: list[int] = []
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: calls.append(1) or chrome_installed)
    reset_browser_cache()
    settings = Settings(linkedin_browser_channel="auto")
    assert resolve_browser(settings) is resolve_browser(settings)
    assert len(calls) == 1
    reset_browser_cache()
    resolve_browser(settings)
    assert len(calls) == 2


def test_a_missing_chrome_error_is_not_cached_so_installing_chrome_is_picked_up(
    monkeypatch: pytest.MonkeyPatch, chrome_absent, tmp_path: Path
) -> None:
    settings = Settings(linkedin_browser_channel="chrome")
    with pytest.raises(BrowserUnavailable):
        resolve_browser(settings)
    app = _fake_app(tmp_path / "later")
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: ChromeInstall(binary=app / "x", app=app))
    assert resolve_browser(settings).channel == "chrome"


def test_find_chrome_prefers_system_applications_then_user_applications_with_executable_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system_app = tmp_path / "System" / "Google Chrome.app"
    monkeypatch.setattr(browser, "MAC_CHROME_APP", system_app)
    home = tmp_path / "home"
    assert find_chrome(platform="darwin", home=home) is None

    user_app = _fake_app(home / "Applications")
    found = find_chrome(platform="darwin", home=home)
    assert found is not None and found.app == user_app and found.needs_executable_path is True

    _fake_app(system_app.parent)
    found = find_chrome(platform="darwin", home=home)
    assert found is not None and found.app == system_app and found.needs_executable_path is False


def test_a_user_applications_chrome_launches_through_executable_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _fake_app(tmp_path / "home" / "Applications")
    install = ChromeInstall(binary=app / "Contents/MacOS/Google Chrome", app=app, needs_executable_path=True)
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: install)
    reset_browser_cache()
    choice = resolve_browser(Settings(linkedin_browser_channel="auto"))
    assert choice.executable_path == str(install.binary)


def test_find_chrome_on_an_unknown_platform_finds_nothing() -> None:
    assert find_chrome(platform="sunos5") is None


def test_chrome_version_reads_the_info_plist_without_starting_chrome(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, version="156.0.7311.42")
    assert chrome_version(ChromeInstall(binary=app / "Contents/MacOS/Google Chrome", app=app)) == "156.0.7311.42"


def test_chrome_version_is_none_for_an_unreadable_plist(tmp_path: Path) -> None:
    app = tmp_path / "Broken.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("not a plist")
    assert chrome_version(ChromeInstall(binary=app / "bin", app=app)) is None
    assert chrome_version(ChromeInstall(binary=tmp_path / "x", app=tmp_path / "Missing.app")) is None


def test_chrome_version_on_linux_parses_dash_dash_version(tmp_path: Path) -> None:
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\necho 'Google Chrome 156.0.8078.48 '\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    assert chrome_version(ChromeInstall(binary=binary)) == "156.0.8078.48"
    assert chrome_version(ChromeInstall(binary=tmp_path / "missing")) is None


def test_a_chrome_with_unknown_version_is_labelled_without_a_major(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(browser, "find_chrome", lambda *a, **k: ChromeInstall(binary=tmp_path / "x", app=tmp_path))
    reset_browser_cache()
    choice = resolve_browser(Settings(linkedin_browser_channel="auto"))
    assert (choice.channel, choice.version, choice.label) == ("chrome", None, "Google Chrome")


def test_chromium_version_comes_from_playwrights_browsers_json(tmp_path: Path) -> None:
    path = tmp_path / "browsers.json"
    path.write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium-headless-shell", "browserVersion": "1.0"},
                    {"name": "chromium", "browserVersion": "156.0.8078.4"},
                ]
            }
        )
    )
    assert chromium_version(path) == "156.0.8078.4"
    path.write_text("{oops")
    assert chromium_version(path) is None
    assert chromium_version(tmp_path / "missing.json") is None


def test_the_installed_playwright_browsers_json_names_a_chromium_version() -> None:
    version = chromium_version()
    assert version is not None and version.split(".")[0].isdigit()

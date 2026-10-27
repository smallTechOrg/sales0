"""``check-browser`` output contract with the resolution and the probe stubbed (pure; the real
browser run is tests/integration/test_check_browser_cli.py)."""

from __future__ import annotations

import threading
import time

import pytest

from sales0 import cli
from sales0.cli import BrowserProbe, Watchdog, automation_tell, check_browser, version_from_product
from sales0.config import Settings
from sales0.scraper import browser
from sales0.scraper.browser import FALLBACK_NOTE, BrowserChoice
from sales0.scraper.errors import BrowserUnavailable

CHROME = BrowserChoice(channel="chrome", version="156.0.8078.48", label="Google Chrome 156", note=None)
FALLBACK = BrowserChoice(channel="chromium", version="156.0.8078.4", label="Built-in browser (Chromium)", note=FALLBACK_NOTE)
CLEAN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"


def _probe(product: str = "Chrome/156.0.8078.48", ua: str = CLEAN_UA, webdriver: bool = False):  # type: ignore[no-untyped-def]
    calls: list[BrowserChoice] = []

    def run(_settings: Settings, choice: BrowserChoice) -> BrowserProbe:
        calls.append(choice)
        return BrowserProbe(product=product, user_agent=ua, webdriver=webdriver, page_url="about:blank")

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.fixture
def resolves_to(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def set_choice(choice: BrowserChoice | Exception) -> None:
        def resolve(_settings: Settings) -> BrowserChoice:
            if isinstance(choice, Exception):
                raise choice
            return choice

        monkeypatch.setattr(browser, "resolve_browser", resolve)

    return set_choice


def test_success_prints_exactly_one_line_on_stdout_and_exits_0(resolves_to, capsys) -> None:  # type: ignore[no-untyped-def]
    resolves_to(CHROME)
    probe = _probe()
    assert check_browser(Settings(), probe=probe) == 0
    out, err = capsys.readouterr()
    assert out == "browser OK channel=chrome version=156.0.8078.48\n"
    assert err == ""
    assert probe.calls == [CHROME]


def test_an_auto_fallback_appends_the_note_to_the_ok_line(resolves_to, capsys) -> None:  # type: ignore[no-untyped-def]
    resolves_to(FALLBACK)
    assert check_browser(Settings(), probe=_probe(product="HeadlessChrome/156.0.8078.4")) == 0
    out, _ = capsys.readouterr()
    assert out == "browser OK channel=chromium version=156.0.8078.4 (Google Chrome not found — using the built-in browser)\n"


def test_a_missing_chrome_is_reported_as_chrome_not_found_on_stderr(resolves_to, capsys) -> None:  # type: ignore[no-untyped-def]
    resolves_to(BrowserUnavailable(browser.CHROME_MISSING_MESSAGE))
    probe = _probe()
    assert check_browser(Settings(), probe=probe) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"browser ERROR type=chrome-not-found {browser.CHROME_MISSING_MESSAGE}\n"
    assert probe.calls == []


def test_a_launch_failure_is_reported_as_launch_failed(resolves_to, capsys) -> None:  # type: ignore[no-untyped-def]
    resolves_to(CHROME)

    def fails(_s: Settings, _c: BrowserChoice) -> BrowserProbe:
        raise BrowserUnavailable("Could not start Google Chrome (boom) — quit any sales0 Chrome window and try again")

    assert check_browser(Settings(), probe=fails) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("browser ERROR type=launch-failed Could not start Google Chrome (boom)")
    assert err.count("\n") == 1


@pytest.mark.parametrize(
    ("ua", "webdriver", "why"),
    [
        (CLEAN_UA, True, "navigator.webdriver is true"),
        (CLEAN_UA.replace("Chrome/", "HeadlessChrome/"), False, "HeadlessChrome"),
    ],
)
def test_an_automation_tell_fails_the_check(resolves_to, capsys, ua: str, webdriver: bool, why: str) -> None:  # type: ignore[no-untyped-def]
    resolves_to(CHROME)
    assert check_browser(Settings(), probe=_probe(ua=ua, webdriver=webdriver)) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("browser ERROR type=automation-tell ") and why in err


def test_version_is_parsed_from_the_cdp_product() -> None:
    assert version_from_product("Chrome/156.0.8078.48") == "156.0.8078.48"
    assert version_from_product("HeadlessChrome/156.0.8078.4") == "156.0.8078.4"
    assert version_from_product("") == ""


def test_a_clean_probe_has_no_automation_tell() -> None:
    assert automation_tell(BrowserProbe("Chrome/1", CLEAN_UA, False, "about:blank")) is None


def test_the_watchdog_fires_when_the_check_hangs_and_never_after_it_finished() -> None:
    fired = threading.Event()
    with Watchdog(0.05, fired.set) as dog:
        assert fired.wait(2)
    assert dog.fired is True

    late = threading.Event()
    with Watchdog(0.2, late.set):
        pass
    time.sleep(0.3)
    assert not late.is_set()


def test_check_browser_hands_the_timeout_handler_to_the_watchdog(resolves_to, capsys) -> None:  # type: ignore[no-untyped-def]
    resolves_to(CHROME)
    fired = threading.Event()

    def hangs(_s: Settings, _c: BrowserChoice) -> BrowserProbe:
        assert fired.wait(2), "watchdog did not fire"
        raise TimeoutError("killed by the watchdog")

    assert check_browser(Settings(), probe=hangs, timeout_s=0.05, on_timeout=fired.set) == 1
    assert fired.is_set()


def test_the_default_watchdog_limit_is_60_seconds() -> None:
    assert cli.CHECK_BROWSER_TIMEOUT_S == 60.0


def test_main_dispatches_check_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_check_browser", lambda: 7)
    assert cli.main(["check-browser"]) == 7


def test_an_invalid_channel_in_the_environment_is_a_clean_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from sales0.config import reset_settings

    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", "firefox")
    reset_settings()
    assert cli.main(["check-browser"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err.startswith("browser ERROR type=launch-failed invalid configuration")

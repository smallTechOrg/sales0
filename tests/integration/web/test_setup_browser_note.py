"""Phase 1.1 Setup copy: the browser line (``#browser-note``) incl. the built-in-browser
fallback and the Chrome-missing error, the connect-in-progress lines (Chrome vs fallback),
the connected copy, the connect-failure error, and the Safety-limits rows. Real app + real
SQLite; the browser resolution is stubbed at ``scraper.browser.resolve_browser``."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from html import unescape

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.jobs.runner import get_runner
from sales0.scraper import browser
from tests.integration.web.conftest import HX, set_session

FALLBACK_NOTE = "Google Chrome not found — using the built-in browser"
CHROME_MISSING = (
    "Google Chrome is not installed — install it from https://www.google.com/chrome/ "
    "or set SALES0_LINKEDIN_BROWSER_CHANNEL=auto"
)


def _card(html: str) -> str:
    html = unescape(html)
    return html.split('id="linkedin-card"', 1)[1].split("</section>", 1)[0]


def _note(card: str) -> str:
    m = re.search(r'<p id="browser-note"[^>]*>(.*?)</p>', card, re.S)
    assert m, "no #browser-note"
    return m.group(0)


def _stub_browser(monkeypatch: pytest.MonkeyPatch, *, channel: str, label: str, note: str | None, version: str | None) -> None:
    choice = browser.BrowserChoice(channel=channel, version=version, label=label, note=note)  # type: ignore[arg-type]
    monkeypatch.setattr(browser, "resolve_browser", lambda settings=None, *a, **k: choice)
    for module in ("sales0.services.session", "sales0.services.runs"):
        monkeypatch.setattr(f"{module}.resolve_browser", browser.resolve_browser, raising=False)


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_browser(monkeypatch, channel="chrome", label="Google Chrome 156", note=None, version="156.0.7311.42")


@pytest.fixture
def fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_browser(monkeypatch, channel="chromium", label="Built-in browser (Chromium)", note=FALLBACK_NOTE, version="140.0")


@pytest.fixture
def busy_connect() -> Iterator[None]:
    release, started = threading.Event(), threading.Event()

    def job() -> None:
        started.set()
        release.wait(30)

    get_runner().submit("connect", job)
    assert started.wait(5)
    yield
    release.set()
    get_runner().wait(10)


def test_not_connected_card_shows_the_chrome_browser_line_and_new_copy(client: TestClient, chrome: None) -> None:
    card = _card(client.get("/setup").text)
    note = _note(card)
    assert "Browser: Google Chrome 156" in note and "outcome--warn" not in note
    assert (
        "sales0 opens Google Chrome with its own sales0 profile on this computer. Log in to LinkedIn there once — "
        "your everyday Chrome profile isn't touched, and sales0 never sees or stores your password."
    ) in card
    assert "Connect LinkedIn" in card and 'id="check-linkedin"' in card
    assert 'id="linkedin-error"' not in card  # no failure to report yet


def test_connect_in_progress_with_chrome_tells_the_user_to_log_in_once(client: TestClient, chrome: None, busy_connect: None) -> None:
    card = _card(client.get("/setup/linkedin/status", headers=HX).text)
    assert "A Google Chrome window opened. Waiting…" in card
    assert "Log in and finish LinkedIn's 2FA in the Chrome window — this is one time only." in card
    assert (
        "It closes by itself a moment after you reach your LinkedIn feed. If Chrome shows “Chrome is being "
        "controlled by automated test software”, that's sales0's window — it's safe to continue."
    ) in card
    assert ">Cancel</button>" in card and 'hx-trigger="every 1s"' in card


def test_fallback_browser_shows_the_warn_note_and_browser_window_copy(client: TestClient, fallback: None, busy_connect: None) -> None:
    card = _card(client.get("/setup").text)
    note = _note(card)
    assert FALLBACK_NOTE in note and "outcome--warn" in note and "Browser:" not in note
    assert "A browser window opened. Waiting…" in card
    assert "Log in and finish LinkedIn's 2FA in the browser window — this is one time only." in card
    assert "in the Chrome window" not in card


def test_chrome_channel_without_chrome_shows_the_browser_unavailable_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real resolution path: channel=chrome with Chrome reported absent → the service's
    BrowserUnavailable label/note reach the card (no stubbed BrowserChoice)."""
    monkeypatch.setenv("SALES0_LINKEDIN_BROWSER_CHANNEL", "chrome")
    reset_settings()

    def missing(*_a: object, **_k: object) -> browser.BrowserChoice:
        from sales0.scraper.errors import BrowserUnavailable

        raise BrowserUnavailable(CHROME_MISSING)

    monkeypatch.setattr(browser, "resolve_browser", missing)
    for module in ("sales0.services.session", "sales0.services.runs"):
        monkeypatch.setattr(f"{module}.resolve_browser", missing, raising=False)
    html = client.get("/setup")
    assert html.status_code == 200
    card = _card(html.text)
    assert CHROME_MISSING in _note(card) and "outcome--warn" in _note(card)
    assert "Google Chrome (not installed)" in unescape(html.text)  # the Safety-limits "Browser" row
    api = client.get("/api/session").json()["data"]
    assert api["browser_label"] == "Google Chrome (not installed)" and api["browser_note"] == CHROME_MISSING


def test_connected_card_copy_and_connect_failure_error(client: TestClient, chrome: None) -> None:
    set_session("connected")
    card = _card(client.get("/setup").text)
    assert "LinkedIn is connected. Checks and runs reuse this login invisibly in the background — no window opens." in card
    assert "Connect again" in card

    set_session("not_connected", last_error="Login not completed — try again")
    card = _card(client.get("/setup").text)
    err = re.search(r'<p id="linkedin-error"[^>]*>(.*?)</p>', card, re.S)
    assert err and "outcome--error" in err.group(0) and "Login not completed — try again" in err.group(1)
    assert card.index('id="linkedin-error"') < card.index('id="connect-linkedin"')  # above the buttons


def test_safety_limits_list_browser_runs_and_both_cooldowns(
    client: TestClient, chrome: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
    monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
    reset_settings()
    html = unescape(client.get("/setup").text)
    caps = html.split('id="caps-title"', 1)[1].split("</section>", 1)[0]
    rows = dict(re.findall(r"<dt>([^<]+)</dt><dd>([^<]*)</dd>", caps))
    assert rows["Browser"] == "Google Chrome 156"
    assert rows["Runs"] == "hidden (headless)"
    assert rows["Cool-down after rate limit / captcha"] == "60 min"
    assert rows["Cool-down after checkpoint / logout"] == "15 min"
    assert "Browser window during runs" not in caps
    assert "Change these in .env" in caps

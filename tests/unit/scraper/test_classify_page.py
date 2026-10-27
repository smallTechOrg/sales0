"""classify_page / classify_url (pure): the ONE classifier for live pages and tests.

Runs over every saved-sample manifest entry's recorded signals, plus URL/status/cookie tables
(architecture.md → Logged-in detection; Saved page samples).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sales0.scraper import selectors
from sales0.scraper.detection import (
    PageSignals,
    PageVerdict,
    classify_page,
    classify_url,
    is_rate_limit_response,
    same_site,
    session_cookie_valid,
    signals_from,
)
from sales0.scraper.errors import AccountSafetyStop, BrowserUnavailable, ScrapeError, SessionMissing
from sales0.scraper.types import ConnectResult, KeywordScrapeResult, SessionCheckResult

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "linkedin_pages"
MANIFEST = json.loads((PAGES / "manifest.json").read_text(encoding="utf-8"))
CASES = MANIFEST["cases"]
LIVE = "https://www.linkedin.com"
FIX = "http://127.0.0.1:43123/__fixtures/linkedin"


def _sig(
    url: str,
    status: int | None = 200,
    cookie: bool = True,
    captcha: bool = False,
    login_form: bool = False,
    authwall: bool = False,
    hints: tuple[str, ...] = (),
) -> PageSignals:
    return PageSignals(
        url=url,
        status=status,
        has_session_cookie=cookie,
        has_captcha=captcha,
        has_login_form=login_form,
        has_authwall=authwall,
        soft_hints=hints,
    )


# --------------------------------------------------------------------------- manifest
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_classify_page_gives_the_manifest_verdict_for_every_saved_sample(case: dict) -> None:
    signals = _sig(case["url"], case["status"], case["cookie"], **{
        "captcha": case["signals"]["has_captcha"],
        "login_form": case["signals"]["has_login_form"],
        "authwall": case["signals"]["has_authwall"],
    })
    verdict = classify_page(signals)
    assert verdict.kind == case["expect"]["kind"]
    if "reason" in case["expect"]:
        assert verdict.reason == case["expect"]["reason"]


def test_manifest_covers_every_required_case() -> None:
    kinds = {c["expect"]["kind"] for c in CASES}
    assert kinds == {"ok", "logout", "checkpoint", "captcha", "rate_limited", "http_error"}
    files = {c["file"] for c in CASES if c["file"]}
    assert files == {p.name for p in PAGES.glob("*.html")}, "every saved page is in the manifest, and vice versa"
    assert {c["status"] for c in CASES if c["file"] is None} == {429, 999, 500}
    reasons = {c["expect"].get("reason") for c in CASES}
    assert {"path:/login", "login_form", "path:/authwall"} <= reasons


def test_logged_in_samples_never_carry_global_nav_or_legacy_classes() -> None:
    logged_in = [c["file"] for c in CASES if c["file"] and c["cookie"] and c["expect"]["kind"] == "ok"]
    assert len(logged_in) == 5  # feed, content (URN layout), empty, drifted, SDUI (Phase 1.2)
    for name in logged_in:
        html = (PAGES / name).read_text(encoding="utf-8")
        assert html.count("global-nav") == 0, name
        assert not re.search(r"update-components-|feed-shared-|search-reusable-", html), name


def test_saved_samples_contain_no_personal_data() -> None:
    emails = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    phones = re.compile(r"\+\d[\d ]{8,}\d")
    for path in PAGES.glob("*.html"):
        html = path.read_text(encoding="utf-8")
        for email in emails.findall(html):
            assert email.endswith(".example"), (path.name, email)
        for phone in phones.findall(html):
            assert re.fullmatch(r"\+91 90000 0000\d", phone), (path.name, phone)


# --------------------------------------------------------------------------- rule table
@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        # 1. rate limit wins over everything
        (_sig(f"{LIVE}/checkpoint/challenge/x", 429, captcha=True), PageVerdict("rate_limited", "status:429")),
        (_sig(f"{LIVE}/feed/", 999, cookie=False), PageVerdict("rate_limited", "status:999")),
        # 2. captcha (marker, or a captcha path) before checkpoint/logout
        (_sig(f"{LIVE}/checkpoint/challenge/AgG", captcha=True), PageVerdict("captcha", "captcha_marker")),
        (_sig(f"{LIVE}/uas/consumer-captcha-v2?x=1"), PageVerdict("captcha", "path:/uas/consumer-captcha-v2")),
        (_sig(f"{LIVE}/feed/", captcha=True, cookie=False), PageVerdict("captcha", "captcha_marker")),
        # 3. checkpoint path
        (_sig(f"{LIVE}/checkpoint/challengesV2/AQ"), PageVerdict("checkpoint", "path:/checkpoint/challengesV2/AQ")),
        (_sig(f"{LIVE}/checkpoint/pk/x", 403), PageVerdict("checkpoint", "path:/checkpoint/pk/x")),
        # 4. logout path (login / challenge-to-login paths)
        (_sig(f"{LIVE}/login", cookie=False), PageVerdict("logout", "path:/login")),
        (_sig(f"{LIVE}/checkpoint/lg/login-submit"), PageVerdict("logout", "path:/checkpoint/lg/login-submit")),
        (_sig(f"{LIVE}/checkpoint/rm/sign-in-another-account?x"), PageVerdict("logout", "path:/checkpoint/rm/sign-in-another-account")),
        (_sig(f"{LIVE}/m/login/"), PageVerdict("logout", "path:/m/login/")),
        (_sig(f"{FIX}/authwall"), PageVerdict("logout", "path:/authwall")),
        # 5. no cookie, on an otherwise fine page
        (_sig(f"{LIVE}/feed/", cookie=False), PageVerdict("logout", "no_cookie")),
        (_sig(f"{LIVE}/feed/", 500, cookie=False), PageVerdict("logout", "no_cookie")),
        # 6. DOM logged-out markers when the URL looks fine
        (_sig(f"{LIVE}/feed/", login_form=True), PageVerdict("logout", "login_form")),
        (_sig(f"{LIVE}/feed/", authwall=True), PageVerdict("logout", "authwall")),
        # 7. other HTTP errors
        (_sig(f"{LIVE}/feed/", 500), PageVerdict("http_error", "status:500")),
        (_sig(f"{LIVE}/search/results/content/?keywords=x", 404), PageVerdict("http_error", "status:404")),
        # 8. ok — no logged-in DOM marker is ever required; unknown status is fine
        (_sig(f"{LIVE}/feed/"), PageVerdict("ok", "ok")),
        (_sig(f"{LIVE}/search/results/content/?keywords=ai", None), PageVerdict("ok", "ok")),
        (_sig(f"{FIX}/feed/", 302), PageVerdict("ok", "ok")),
    ],
)
def test_classify_page_rules_apply_in_order(signals: PageSignals, expected: PageVerdict) -> None:
    assert classify_page(signals) == expected


def test_soft_hints_never_change_the_verdict() -> None:
    hints = tuple(selectors.SOFT_LOGGED_IN_HINTS)
    assert classify_page(_sig(f"{LIVE}/feed/", hints=())) == classify_page(_sig(f"{LIVE}/feed/", hints=hints))
    assert classify_page(_sig(f"{LIVE}/feed/", cookie=False, hints=hints)).kind == "logout"


def test_a_keyword_that_looks_like_a_safety_path_never_trips_the_classifier() -> None:
    url = f"{LIVE}/search/results/content/?keywords=login%20checkpoint%20captcha%20authwall"
    assert classify_url(url) is None
    assert classify_page(_sig(url)).kind == "ok"


# --------------------------------------------------------------------------- classify_url
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{LIVE}/login", "logout"),
        (f"{LIVE}/LOGIN?session_redirect=x", "logout"),
        (f"{LIVE}/login-submit", "logout"),
        (f"{LIVE}/uas/login?x", "logout"),
        (f"{LIVE}/authwall?trk=x", "logout"),
        (f"{LIVE}/signup/cold-join", "logout"),
        (f"{LIVE}/signin", "logout"),
        (f"{LIVE}/reg/join", "logout"),
        (f"{LIVE}/checkpoint/lg/login-submit", "logout"),
        (f"{LIVE}/m/login", "logout"),
        (f"{LIVE}/checkpoint/challenge/AgF", "checkpoint"),
        (f"{LIVE}/checkpoint/challengesV2/x", "checkpoint"),
        (f"{FIX}/checkpoint/challenge/remember-browser", "checkpoint"),
        (f"{FIX}/checkpoint/challenge/verify", "checkpoint"),
        (f"{LIVE}/uas/consumer-captcha-v2", "captcha"),
        (f"{FIX}/login", "logout"),
        (f"{FIX}/authwall", "logout"),
        (f"{LIVE}/feed/", None),
        (f"{LIVE}/in/someone-login/", None),  # only the first segment counts for login paths
        (f"{LIVE}/search/results/content/?keywords=login", None),
        (f"{FIX}/feed/", None),
        (f"{FIX}/search/results/content/?keywords=checkpoint-test", None),
        ("about:blank", None),
        ("data:text/html,<p>login</p>", None),
        ("chrome-error://chromewebdata/", None),
    ],
)
def test_classify_url_is_path_only(url: str, expected: str | None) -> None:
    assert classify_url(url) == expected


def test_classify_url_with_a_base_ignores_other_sites() -> None:
    assert classify_url("https://accounts.example.com/login", LIVE) is None
    assert classify_url(f"{LIVE}/login", LIVE) == "logout"
    assert classify_url(f"{FIX}/authwall", FIX) == "logout"
    assert classify_url("http://127.0.0.1:9/__fixtures/linkedin/authwall", FIX) is None


# --------------------------------------------------------------------------- cookie
NOW = 1_800_000_000.0


@pytest.mark.parametrize(
    ("cookies", "expected"),
    [
        ([{"name": "li_at", "value": "AQED", "expires": -1}], True),  # session cookie
        ([{"name": "li_at", "value": "AQED", "expires": NOW + 60}], True),
        ([{"name": "li_at", "value": "AQED", "expires": NOW - 1}], False),  # expired
        ([{"name": "li_at", "value": "", "expires": -1}], False),  # empty value
        ([{"name": "JSESSIONID", "value": "ajax:1", "expires": -1}], False),
        ([], False),
        ([{"name": "li_at", "value": "old", "expires": NOW - 5}, {"name": "li_at", "value": "new", "expires": NOW + 5}], True),
    ],
)
def test_session_cookie_must_be_a_non_empty_non_expired_li_at(cookies: list[dict], expected: bool) -> None:
    assert session_cookie_valid(cookies, now=NOW) is expected


def test_signals_from_builds_signals_and_extras_from_one_evaluation() -> None:
    dom = {"has_captcha": False, "has_login_form": True, "has_authwall": False, "no_results": True,
           "cards_found": 3, "soft_hints": ["main_landmark"]}
    signals, extras = signals_from(f"{LIVE}/feed/", 200, True, dom)
    assert signals == _sig(f"{LIVE}/feed/", 200, True, login_form=True, hints=("main_landmark",))
    assert extras == {"no_results": True, "cards_found": 3}
    empty, extras = signals_from("about:blank", None, False, {})
    assert (empty.has_captcha, empty.soft_hints, extras) == (False, (), {"no_results": False, "cards_found": 0})


def test_signals_from_passes_the_card_strategy_and_sdui_screen_through_as_information_only() -> None:
    dom = {"has_captcha": False, "has_login_form": False, "has_authwall": False, "no_results": False,
           "cards_found": 6, "strategy": "sdui", "sdui_screen": True, "soft_hints": []}
    signals, extras = signals_from(f"{LIVE}/search/results/content/?keywords=ai", 200, True, dom)
    assert extras == {"no_results": False, "cards_found": 6, "strategy": "sdui", "sdui_screen": True}
    assert classify_page(signals) == PageVerdict("ok", "ok")  # never part of the verdict
    _, none = signals_from(f"{LIVE}/feed/", 200, True, {**dom, "strategy": None, "sdui_screen": 0})
    assert (none["strategy"], none["sdui_screen"]) == ("none", False)


def test_the_sdui_sample_is_a_logged_in_results_page_in_the_manifest() -> None:
    sdui = next(c for c in CASES if c["id"] == "search_results_sdui")
    assert sdui["file"] == "search_results_sdui.html" and sdui["cookie"] is True
    assert sdui["expect"]["kind"] == "ok" and sdui["expect"]["strategy"] == "sdui"
    assert classify_page(_sig(sdui["url"], sdui["status"], sdui["cookie"])) == PageVerdict("ok", "ok")


def test_no_logged_in_dom_marker_exists_anymore() -> None:
    assert not hasattr(selectors, "LOGGED_IN_MARKER")


# --------------------------------------------------------------------------- site rules
@pytest.mark.parametrize(
    ("url", "status", "base", "expected"),
    [
        ("https://www.linkedin.com/voyager/api/search/dash/clusters?q=x", 429, LIVE, True),
        ("https://www.linkedin.com/voyager/api/graphql", 999, LIVE, True),
        ("https://linkedin.com/feed/", 429, LIVE, True),
        ("https://static.licdn.com/sc/h/x.js", 429, LIVE, False),  # CDN, not the session
        ("https://evil-linkedin.com/x", 429, LIVE, False),
        ("https://www.linkedin.com/voyager/api/graphql", 500, LIVE, False),
        (f"{FIX}/voyager/api/x", 429, FIX, True),
        ("http://127.0.0.1:9999/other", 429, FIX, False),
        ("data:text/plain,hi", 429, LIVE, False),
    ],
)
def test_rate_limit_responses_count_only_from_the_linkedin_site(url: str, status: int, base: str, expected: bool) -> None:
    assert is_rate_limit_response(url, status, base) is expected


def test_same_site_rejects_malformed_urls() -> None:
    assert same_site("http://[::1", LIVE) is False


# --------------------------------------------------------------------------- errors + types contract
def test_session_missing_is_neither_a_safety_stop_nor_a_scrape_error() -> None:
    err = SessionMissing(f"{LIVE}/feed/")
    assert err.url == f"{LIVE}/feed/"
    assert err.message == "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"
    assert not isinstance(err, (ScrapeError, AccountSafetyStop))


def test_browser_unavailable_is_a_scrape_error_and_a_stop_is_not() -> None:
    assert isinstance(BrowserUnavailable("Google Chrome is not installed"), ScrapeError)
    assert not isinstance(AccountSafetyStop("logout", f"{LIVE}/authwall"), ScrapeError)


def test_phase_1_1_result_fields_default_safely() -> None:
    result = KeywordScrapeResult(keyword="k", posts=[], scrolls=0, parse_errors=0, drift_warning=False)
    assert (result.cards_found, result.no_results, result.field_misses) == (0, False, {})
    other = KeywordScrapeResult(keyword="k", posts=[], scrolls=0, parse_errors=0, drift_warning=False)
    assert result.field_misses is not other.field_misses  # default_factory, never shared
    assert SessionCheckResult(logged_in=True, reason="ok", url="u").soft_hints == ()
    assert ConnectResult(connected=True, reason="ok", detail=None).browser is None
    assert ConnectResult(connected=True, reason="ok", detail=None, browser="chrome").browser == "chrome"

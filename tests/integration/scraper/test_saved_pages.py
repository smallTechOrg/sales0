"""Saved LinkedIn page samples in real headless Chromium (architecture.md → Saved page samples).

Each manifest case is served at its manifest URL through ``context.route`` → ``fulfill``.
EVERY other request is aborted, and the browser's host resolver maps every name to
NOTFOUND, so no test can ever reach real linkedin.com. The real ``SIGNALS_JS``,
``EXTRACT_JS``, ``SEE_MORE_JS``, ``classify_page`` and ``post_from_raw`` run on each page.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Response, Route, sync_playwright

from sales0.scraper.detection import classify_page, read_signals
from sales0.scraper.parsing import drift_detected, parse_cards
from sales0.scraper.search import extract_raw_cards, see_more_buttons

pytestmark = pytest.mark.browser

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "linkedin_pages"
MANIFEST = json.loads((PAGES / "manifest.json").read_text(encoding="utf-8"))
CASES = MANIFEST["cases"]
BASE = MANIFEST["base"]
TODAY = date.fromisoformat(MANIFEST["today"])
CONTENT = next(c for c in CASES if c["id"] == "search_results_content")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, chromium_sandbox=True, args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            yield b
        finally:
            b.close()


class Served:
    def __init__(self, context: BrowserContext, page: Page, response: Response | None, requests: list[str]) -> None:
        self.context, self.page, self.response, self.requests = context, page, response, requests


@pytest.fixture
def serve(browser: Browser) -> Iterator:
    contexts: list[BrowserContext] = []

    def _serve(case: dict) -> Served:
        context = browser.new_context()
        contexts.append(context)
        if case["cookie"]:
            context.add_cookies([{
                "name": "li_at", "value": "saved-sample-session", "domain": ".linkedin.com", "path": "/",
                "expires": time.time() + 365 * 86400, "httpOnly": True, "secure": True,
            }])
        body = (PAGES / case["file"]).read_text(encoding="utf-8") if case["file"] else "<html><body></body></html>"
        requests: list[str] = []

        def handler(route: Route) -> None:
            requests.append(route.request.url)
            if route.request.url == case["url"]:
                route.fulfill(status=case["status"], body=body, content_type="text/html; charset=utf-8")
            else:
                route.abort()  # never let anything leave the browser

        context.route("**/*", handler)
        page = context.new_page()
        response = page.goto(case["url"], wait_until="domcontentloaded")
        return Served(context, page, response, requests)

    yield _serve
    for context in contexts:
        context.close()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_saved_sample_classifies_as_the_manifest_says_in_a_real_browser(serve, case: dict) -> None:
    served = serve(case)
    signals, extras = read_signals(served.page, served.response, served.context, BASE)

    assert signals.status == case["status"]
    assert signals.has_session_cookie is case["cookie"]
    recorded = case["signals"]
    assert (signals.has_captcha, signals.has_login_form, signals.has_authwall) == (
        recorded["has_captcha"], recorded["has_login_form"], recorded["has_authwall"]
    ), "the recorded signals used by the pure test must match what SIGNALS_JS reads"
    verdict = classify_page(signals)
    expect = case["expect"]
    assert verdict.kind == expect["kind"]
    assert verdict.reason == expect["reason"]
    if expect.get("soft_hints_nonempty"):
        assert signals.soft_hints, "a logged-in page reports soft hints (logged only)"
    if "no_results" in expect:
        assert extras["no_results"] is expect["no_results"]
    if "cards_found" in expect:
        assert extras["cards_found"] == expect["cards_found"]
    assert served.requests[0] == case["url"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if "posts" in c["expect"]], ids=[c["id"] for c in CASES if "posts" in c["expect"]]
)
def test_results_samples_parse_to_the_pinned_posts_and_drift_verdict(serve, case: dict) -> None:
    served = serve(case)
    _, extras = read_signals(served.page, served.response, served.context, BASE)
    parsed = parse_cards(extract_raw_cards(served.page), BASE, TODAY, cap=15)
    expect = case["expect"]

    assert len(parsed.posts) == expect["posts"]
    assert parsed.parse_errors == 0
    assert parsed.cards_found == expect["cards_found"]
    assert parsed.field_misses == expect["field_misses"]
    assert drift_detected(len(parsed.posts), max(parsed.cards_found, extras["cards_found"]), extras["no_results"]) is expect["drift"]
    if "urns" in expect:
        assert [p.urn for p in parsed.posts] == expect["urns"]


def test_the_current_markup_sample_yields_every_pinned_field(serve) -> None:
    served = serve(CONTENT)
    raws = extract_raw_cards(served.page)
    posts = {p.urn: p for p in parse_cards(raws, BASE, TODAY, cap=15).posts}
    for urn in CONTENT["expect"]["never_urns"]:
        assert urn not in posts, "a reshared original nested in its card is never a second post"
    assert {raw["via"]["urn"] for raw in raws} == {"attr", "feed_update_href", "posts_href"}

    for urn, want in CONTENT["expect"]["fields"].items():
        post = posts[urn]
        assert post.author_name == want["author_name"], urn
        assert post.author_profile_url == want["author_profile_url"], urn
        assert post.author_headline == want["author_headline"], urn
        assert post.posted_relative == want["posted_relative"], urn
        assert post.posted_date is not None, urn
        assert want["text_contains"] in post.text, urn
        if "text_excludes" in want:
            assert want["text_excludes"] not in post.text, urn
        assert post.url == f"{BASE}/feed/update/{urn}/"
        assert "see more" not in post.text.lower() and not post.text.endswith("more")


def test_see_more_finds_the_truncated_post_toggle_and_reveals_its_tail(serve) -> None:
    served = serve(CONTENT)
    truncated = "urn:li:activity:7390000000000000101"
    tail = CONTENT["expect"]["fields"][truncated]["text_after_expand_contains"]
    before = {p.urn: p for p in parse_cards(extract_raw_cards(served.page), BASE, TODAY).posts}
    assert tail not in before[truncated].text

    buttons = see_more_buttons(served.page, 6)
    assert len(buttons) == 1, "only the truncated post has a toggle; menu/follow/social buttons never match"
    assert "see more" in (buttons[0].get_attribute("aria-label") or "")
    buttons[0].click()

    after = {p.urn: p for p in parse_cards(extract_raw_cards(served.page), BASE, TODAY).posts}
    assert tail in after[truncated].text
    assert after[truncated].text.startswith(before[truncated].text)
    assert see_more_buttons(served.page, 6) == []


def test_no_saved_sample_request_ever_leaves_the_browser(serve) -> None:
    served = serve(next(c for c in CASES if c["id"] == "captcha_challenge"))
    served.page.wait_for_timeout(300)  # let the captcha iframe try to load
    assert served.requests[0] == next(c for c in CASES if c["id"] == "captcha_challenge")["url"]
    assert any("arkoselabs" in url for url in served.requests[1:]), "the iframe request was intercepted (and aborted)"

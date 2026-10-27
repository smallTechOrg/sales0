"""Saved LinkedIn page samples in real headless Chromium (architecture.md → Saved page samples).

Each manifest case is served at its manifest URL through ``context.route`` → ``fulfill``.
EVERY other request is aborted, and the browser's host resolver maps every name to
NOTFOUND, so no test can ever reach real linkedin.com. The real ``SIGNALS_JS``,
``EXTRACT_JS``, ``SEE_MORE_JS``, ``classify_page`` and ``post_from_raw`` run on each page.

Phase 1.2 manifest keys (optional): ``expect.strategy``, ``expect.refs`` (source refs in DOM
order; ``urns`` keeps meaning the same for the legacy samples), ``expect.link_kinds`` (ref →
kind) and ``fields`` keyed by ref. The SDUI sample (S1, sanitised from one real capture) must
parse to its 6 pinned posts through the ``sdui`` strategy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Response, Route, sync_playwright

from sales0.scraper import selectors as S
from sales0.scraper.detection import classify_page, read_signals
from sales0.scraper.parsing import drift_detected, parse_cards
from sales0.scraper.relative_time import normalize_relative
from sales0.scraper.search import count_cards, extract_raw_cards, see_more_buttons
from sales0.scraper.urls import link_kind_of

pytestmark = pytest.mark.browser

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "linkedin_pages"
MANIFEST = json.loads((PAGES / "manifest.json").read_text(encoding="utf-8"))
CASES = MANIFEST["cases"]
BASE = MANIFEST["base"]
TODAY = date.fromisoformat(MANIFEST["today"])
CONTENT = next(c for c in CASES if c["id"] == "search_results_content")
SDUI = next(c for c in CASES if c["id"] == "search_results_sdui")


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
    if "strategy" in expect:
        assert extras["strategy"] == expect["strategy"]
    assert served.requests[0] == case["url"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if "posts" in c["expect"]], ids=[c["id"] for c in CASES if "posts" in c["expect"]]
)
def test_results_samples_parse_to_the_pinned_posts_and_drift_verdict(serve, case: dict) -> None:
    served = serve(case)
    _, extras = read_signals(served.page, served.response, served.context, BASE)
    parsed = parse_cards(extract_raw_cards(served.page), BASE, TODAY, cap=15, search_url=case["url"])
    expect = case["expect"]

    assert len(parsed.posts) == expect["posts"]
    assert parsed.parse_errors == 0
    assert parsed.cards_found == expect["cards_found"]
    assert parsed.field_misses == expect["field_misses"]
    assert drift_detected(len(parsed.posts), max(parsed.cards_found, extras["cards_found"]), extras["no_results"]) is expect["drift"]
    assert count_cards(served.page) == extras["cards_found"], "COUNT_CARDS_JS and SIGNALS_JS count the same thing"
    if "urns" in expect:
        assert [p.urn for p in parsed.posts] == expect["urns"]
        assert [p.source_ref for p in parsed.posts] == expect["urns"]
    if "refs" in expect:
        assert [p.source_ref for p in parsed.posts] == expect["refs"]
    if "strategy" in expect:
        assert parsed.strategy == expect["strategy"]
    if "link_kinds" in expect:
        assert {p.source_ref: p.link_kind for p in parsed.posts} == expect["link_kinds"]
    for post in parsed.posts:
        assert post.link_kind == link_kind_of(post.url) and post.search_url == case["url"]


def test_the_legacy_urn_samples_still_parse_through_the_urn_strategy(serve) -> None:
    served = serve(CONTENT)
    _, extras = read_signals(served.page, served.response, served.context, BASE)
    raws = extract_raw_cards(served.page)
    parsed = parse_cards(raws, BASE, TODAY, cap=15)
    assert parsed.strategy == extras["strategy"] == "urn"
    assert {raw["strategy"] for raw in raws} == {"urn"}
    assert all(raw["component_key"] is None and raw["has_control_menu"] is False for raw in raws)
    assert all(p.link_kind == "post" and p.urn == p.source_ref for p in parsed.posts)


def test_the_sdui_sample_yields_every_pinned_field_through_the_sdui_strategy(serve) -> None:
    served = serve(SDUI)
    signals, extras = read_signals(served.page, served.response, served.context, BASE)
    assert classify_page(signals).kind == "ok"
    assert (extras["strategy"], extras["sdui_screen"]) == ("sdui", True)
    raws = extract_raw_cards(served.page)
    assert len(raws) == 6 and {raw["strategy"] for raw in raws} == {"sdui"}
    assert all(raw["urn"] is None and raw["post_href"] is None for raw in raws), "the SDUI DOM carries no URN"
    assert [raw["has_control_menu"] for raw in raws] == [True] * 6
    parsed = parse_cards(raws, BASE, TODAY, cap=15, search_url=SDUI["url"])
    posts = {p.source_ref: p for p in parsed.posts}
    raw_by_ref = {f"ck:{raw['component_key']}": raw for raw in raws}

    for ref, want in SDUI["expect"]["fields"].items():
        post, raw = posts[ref], raw_by_ref[ref]
        assert post.urn is None and ref.startswith("ck:")
        assert post.author_name == want["author_name"], ref
        assert post.author_profile_url == want["author_profile_url"], ref
        assert post.author_headline == want["author_headline"], ref
        assert raw["posted_relative"] == want["posted_relative"], ref  # the actor line as read
        assert post.posted_relative == normalize_relative(want["posted_relative"]), ref
        assert post.posted_date is not None, ref
        assert want["text_contains"] in post.text, ref
        # No URN and no copied link on a static page: the card links to the author, labelled honestly.
        assert (post.url, post.link_kind) == (want["author_profile_url"], "profile"), ref
        assert not post.text.rstrip().endswith("more") and not post.text.endswith("…"), ref
        assert raw["via"]["strategy"] == "sdui" and raw["via"]["text"] in S.SDUI_TEXT, ref


def test_the_sdui_sample_offers_its_expand_buttons_first_and_never_a_menu_or_social_button(serve) -> None:
    served = serve(SDUI)
    buttons = see_more_buttons(served.page, 6)
    assert buttons, "the SDUI sample keeps the posts' expandable-text buttons"
    for button in buttons:
        assert button.evaluate("(el, css) => el.matches(css)", S.SDUI_EXPAND), "only the SDUI …more button is chosen"
        assert button.evaluate("(el, css) => !el.closest(css)", S.SDUI_CONTROL_MENU)
    menus = served.page.evaluate(
        "(css) => Array.from(document.querySelectorAll(css)).map((m) => (m.textContent || '').toLowerCase())",
        S.SDUI_CONTROL_MENU,
    )
    assert any(S.COPY_LINK_TEXT in text for text in menus), "a control menu with Copy link to post is in the sample"


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

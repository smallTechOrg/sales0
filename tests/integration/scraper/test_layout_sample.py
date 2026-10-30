"""Layout samples: the cleaned copy of a result page kept when a keyword ends with a layout warning.

Real headless Chromium, no network: the sanitiser must keep the structure a selector fix needs (data-view-name,
data-testid, role, componentkey, aria-*, hrefs without their query) and drop everything else (scripts, styles,
images, inline handlers, cookies-bearing URLs), with every text node and the whole sample capped.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, sync_playwright

from sales0.scraper import layout_sample as L

pytestmark = pytest.mark.browser

LONG = "word " * 400
CARD = f"""
<div role="listitem" componentkey="expandedKEY1FeedType_FLAGSHIP_SEARCH" onclick="steal()" style="color:red" data-secret="x">
  <script>window.leak = document.cookie</script><style>.a{{}}</style>
  <div data-view-name="feed-full-update-NEW" class="a1b2c3">
    <a href="https://www.linkedin.com/in/jane-doe/?miniProfileUrn=urn%3Ali%3Afs&trk=abc#frag" onmouseover="x()">Jane Doe</a>
    <img src="https://media.licdn.com/a.jpg" alt="Jane"><svg><path d="M0"/></svg>
    <p aria-label="Posted 3h ago"><time datetime="2026-10-30">3h</time></p>
    <span data-testid="post-body-v2">{LONG}</span>
    <input name="csrf" value="token"><!-- an HTML comment -->
  </div>
</div>"""
PAGE = "<html><head><title>Search | LinkedIn</title></head><body><main>" + CARD * 5 + "</main></body></html>"


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            yield b
        finally:
            b.close()


def test_sample_keeps_structure_and_drops_everything_risky(browser: Browser) -> None:
    page = browser.new_page()
    page.set_content(PAGE)
    s = L.capture(page, reason="0 posts parsed; no posts parsed")
    assert s is not None and s["strategy"] in ("listitem", "listitem_fallback")
    html = s["html"]
    assert s["cards_total"] == 5 and s["cards_sampled"] == L.MAX_CARDS and html.count('role="listitem"') == L.MAX_CARDS
    for kept in ('data-view-name="feed-full-update-NEW"', 'data-testid="post-body-v2"', 'componentkey="expandedKEY1',
                 'aria-label="Posted 3h ago"', 'datetime="2026-10-30"', 'href="https://www.linkedin.com/in/jane-doe/"'):
        assert kept in html, kept
    for gone in ("<script", "document.cookie", "<style", "<img", "<svg", "<input", "csrf", "token", "onclick", "onmouseover",
                 "style=", "data-secret", "miniProfileUrn", "trk=", "#frag", "an HTML comment", "media.licdn.com"):
        assert gone not in html, gone
    assert "word word" in html and "word " * 100 not in html  # long text cut to MAX_TEXT
    assert s["title"] == "Search | LinkedIn" and s["counts"]["role_listitem"] == 5 and s["counts"]["time"] == 5


def test_sample_falls_back_to_main_children_and_caps_its_size(browser: Browser) -> None:
    page = browser.new_page()
    page.set_content("<html><body><main>" + "".join(f"<section><p>{'x' * 250}</p>" + "<div><span>y</span></div>" * 3000 + "</section>" for _ in range(3)) + "</main></body></html>")
    s = L.capture(page, reason="0 posts parsed")
    assert s is not None and s["strategy"] == "main" and len(s["html"]) <= L.MAX_HTML + 200
    assert "sample cut at" in s["html"]


def test_capture_never_raises_on_a_closed_page(browser: Browser) -> None:
    page = browser.new_page()
    page.close()
    assert L.capture(page, reason="x") is None


@pytest.mark.parametrize(
    ("facts", "want"),
    [
        (dict(posts=0, parse_errors=0, drift=True, field_misses={}), True),
        (dict(posts=5, parse_errors=1, drift=False, field_misses={}), True),
        (dict(posts=4, parse_errors=0, drift=False, field_misses={"author_name": 2}), True),
        (dict(posts=4, parse_errors=0, drift=False, field_misses={"author_name": 1}), False),
        (dict(posts=3, parse_errors=0, drift=False, field_misses={"text": 3}), True),
        (dict(posts=1, parse_errors=0, drift=False, field_misses={"posted_relative": 1}), False),  # fewer than 2 posts
        (dict(posts=6, parse_errors=0, drift=False, field_misses={"something_else": 6}), False),
        (dict(posts=0, parse_errors=0, drift=False, field_misses={}), False),  # a clean "no results" page
    ],
)
def test_needs_sample_matches_the_keyword_warning_rule(facts: dict, want: bool) -> None:
    assert L.needs_sample(**facts) is want

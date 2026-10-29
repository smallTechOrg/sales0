"""Phase 1.3: the marker-free LinkedIn results variant (no data-view-name anywhere).

Synthetic, sanitised markup with the structure of the live variant: role=listitem cards holding a
hidden "Feed post" heading, a profile link, a time, an expandable text box and an
"Open control menu for post by <Name>" button. Real headless Chromium, no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from playwright.sync_api import Browser, sync_playwright

from sales0.scraper.parsing import drift_detected, parse_cards
from sales0.scraper.search import count_cards, extract_raw_cards

pytestmark = pytest.mark.browser

BASE = "https://www.linkedin.com"


def _card(key: str, slug: str, name: str, headline: str, rel: str, text: str, more: bool = False) -> str:
    more_btn = (
        '<button type="button" data-testid="expandable-text-button"><span>…</span><span>more</span></button>' if more else ""
    )
    return f"""
<div role="listitem" componentkey="update-card-focus{key}">
 <div data-display-contents="true"><div componentkey="{key}">
  <h2><span>Feed post</span><span aria-hidden="true"></span></h2>
  <div><a href="https://www.linkedin.com/in/{slug}/?miniProfileUrn=x" tabindex="0"><figure><img alt="View {name}’s profile"></figure></a></div>
  <div><div>
    <a href="https://www.linkedin.com/in/{slug}/"><div aria-label="{name} 3rd+"></div></a>
    <p><span>{headline}</span></p>
    <p><span>{rel}</span></p>
    <button type="button" aria-label="Follow {name}"><span>Follow</span></button>
  </div></div>
  <button type="button" aria-label="Open control menu for post by {name}"><span></span></button>
 </div>
 <p><span tabindex="-1" data-testid="expandable-text-box">{text}{more_btn}</span></p>
 <div><button type="button" aria-label="Comment"></button></div>
 </div>
</div>"""


PAGE = (
    '<html><body><main><div data-sdui-screen="com.linkedin.sdui.flagshipnav.search.SearchResultsContent">'
    + _card("kA1", "jane-doe-1", "Jane Doe", "Head of L&D at Acme", "24m", "We are looking for an AI training partner for 200 staff in Pune.")
    + _card("kB2", "sam-roe-2", "Sam Roe", "Recruiter | Staffing", "3h", "Hiring freelance trainers for an agentic AI cohort.", more=True)
    + _card("kC3", "lee-poe-3", "Lee Poe", "Founder", "2d", "Thoughts on agentic AI.")
    + "</div></main></body></html>"
)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            yield b
        finally:
            b.close()


def test_listitem_variant_parses_every_post(browser: Browser) -> None:
    page = browser.new_page()
    page.set_content(PAGE)
    assert count_cards(page) == 3
    raws = extract_raw_cards(page)
    assert [r["strategy"] for r in raws] == ["listitem"] * 3
    parsed = parse_cards(raws, BASE, date(2026, 10, 28))
    assert parsed.strategy == "listitem"
    assert len(parsed.posts) == 3 and parsed.parse_errors == 0
    assert not drift_detected(len(parsed.posts), parsed.cards_found, no_results=False)
    first, second, _third = parsed.posts
    assert first.author_name == "Jane Doe"
    assert first.author_headline == "Head of L&D at Acme"
    assert first.text.startswith("We are looking for an AI training partner")
    assert "in/jane-doe-1" in (first.author_profile_url or "")
    assert second.text == "Hiring freelance trainers for an agentic AI cohort."  # "…more" stripped
    assert len({p.source_ref for p in parsed.posts}) == 3


def test_listitem_tier_used_when_sdui_markers_yield_no_text(browser: Browser) -> None:
    # data-view-name cards present but empty, real content in listitems: the text-bearing tier wins.
    html = PAGE.replace("<main>", '<main><div data-view-name="feed-full-update"></div>')
    page = browser.new_page()
    page.set_content(html)
    raws = extract_raw_cards(page)
    assert raws and all(r["strategy"] == "listitem" for r in raws)
    assert sum(1 for r in raws if r["text"]) == 3

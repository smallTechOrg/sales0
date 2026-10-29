"""Phase 1.4: parser completeness audit on synthetic markup (real headless Chromium, no network).

For BOTH the SDUI and the marker-free listitem tiers: name, headline, profile URL (person and
company), time, the full text incl. the "see more" tail, article / document / image posts (their
titles and captions are not the post text), reshares, external links, e-mails and phones (also in
mailto:/tel: anchors), non-English text, and empty results.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from sales0.scraper.parsing import drift_detected, parse_cards
from sales0.scraper.search import count_cards, extract_raw_cards

pytestmark = pytest.mark.browser

BASE = "https://www.linkedin.com"
TODAY = date(2026, 10, 28)


def sdui(key: str, name: str, slug: str, headline: str, rel: str, body: str, kind: str = "in", media: str = "", inner: str = "") -> str:
    return f"""
<div role="listitem" componentkey="expanded{key}FeedType_FLAGSHIP_SEARCH"><div componentkey="{key}"><div data-view-name="feed-full-update">
 <div data-view-name="feed-actor"><a href="{BASE}/{kind}/{slug}/" aria-label="View {name}’s profile"><img alt=""></a></div>
 <div data-view-name="feed-actor"><a href="{BASE}/{kind}/{slug}/"><span>{name}</span></a><span>• 2nd</span><p>{headline}</p><p><span>{rel} •</span></p></div>
 <div data-view-name="feed-control-menu"><button type="button" aria-label="Open control menu for post by {name}">…</button></div>
 <div data-view-name="feed-commentary"><span data-testid="expandable-text-box">{body}</span></div>
 {media}{inner}
</div></div></div>"""


def listitem(key: str, name: str, slug: str, headline: str, rel: str, body: str, kind: str = "in", media: str = "") -> str:
    return f"""
<div role="listitem" componentkey="update-card-focus{key}"><div componentkey="{key}">
 <h2><span>Feed post</span></h2>
 <a href="{BASE}/{kind}/{slug}/?miniProfileUrn=x"><figure><img alt="View {name}’s profile"></figure></a>
 <div><a href="{BASE}/{kind}/{slug}/"><div aria-label="{name} 3rd+"></div></a><p><span>{headline}</span></p><p><span>{rel}</span></p>
 <button type="button" aria-label="Follow {name}"><span>Follow</span></button></div>
 <button type="button" aria-label="Open control menu for post by {name}"><span></span></button>
 </div>
 <p><span data-testid="expandable-text-box">{body}</span></p>{media}
</div>"""


BUILDERS = {"sdui": sdui, "listitem": listitem}
ARTICLE = '<div data-view-name="feed-article"><a href="https://example.invalid/a"><span dir="ltr">Article title that must not be the post text at all</span></a></div>'
DOCUMENT = '<div data-view-name="feed-document-container"><span dir="ltr">Document caption slides 1 of 12 should be ignored</span></div>'
IMAGE = '<div data-view-name="feed-update-image"><img alt="Image alt text that is not the post"><span dir="ltr">Image caption text</span></div>'


def page_of(cards: str) -> str:
    return f'<html><body><main><div data-sdui-screen="com.linkedin.sdui.flagshipnav.search.SearchResultsContent">{cards}</div></main></body></html>'


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    p = browser.new_page()
    yield p
    p.close()


def parse(page: Page, html: str):
    page.set_content(page_of(html))
    return parse_cards(extract_raw_cards(page), BASE, TODAY)


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
def test_person_and_company_authors_expose_name_headline_profile_and_time(page: Page, tier: str) -> None:
    card = BUILDERS[tier]
    parsed = parse(
        page,
        card("kP1", "Jane Doe", "jane-doe-1", "Head of L&D at Acme", "3h", "We need an AI trainer for 40 managers.")
        + card("kC2", "Acme Corp", "acme-corp", "12,345 followers", "1w", "Acme is looking for a GenAI workshop partner.", kind="company"),
    )
    assert parsed.strategy == tier and parsed.parse_errors == 0 and len(parsed.posts) == 2
    jane, acme = parsed.posts
    assert (jane.author_name, jane.author_headline) == ("Jane Doe", "Head of L&D at Acme")
    assert jane.author_profile_url == f"{BASE}/in/jane-doe-1/"
    assert jane.posted_relative == "3h" and jane.posted_date == TODAY
    assert acme.author_name == "Acme Corp" and acme.author_profile_url == f"{BASE}/company/acme-corp/"
    assert acme.posted_relative == "1w" and acme.posted_date is not None


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
@pytest.mark.parametrize("media", [ARTICLE, DOCUMENT, IMAGE], ids=["article", "document", "image"])
def test_media_posts_keep_the_commentary_and_drop_the_media_text(page: Page, tier: str, media: str) -> None:
    parsed = parse(page, BUILDERS[tier]("kM1", "Sam Roe", "sam-roe", "Founder", "2d", "Looking for a prompt engineering trainer for my team.", media=media))
    assert len(parsed.posts) == 1
    text = parsed.posts[0].text
    assert text == "Looking for a prompt engineering trainer for my team."
    assert "must not be" not in text and "caption" not in text.lower() and "alt text" not in text


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
def test_email_phone_and_external_links_are_kept(page: Page, tier: str) -> None:
    body = (
        'Send proposals to <a href="mailto:ops@acme.example">ops@acme.example</a> or call '
        '+91 98200 11111. Details: <a href="https://lnkd.in/abc123">https://lnkd.in/abc123</a> '
        'Questions? <a href="mailto:hidden@acme.example">email me</a> '
        '<a href="tel:+919820022222">call me</a>'
    )
    text = parse(page, BUILDERS[tier]("kE1", "Eve Ng", "eve-ng", "COO", "1d", body)).posts[0].text
    assert "ops@acme.example" in text and "+91 98200 11111" in text and "https://lnkd.in/abc123" in text
    assert "hidden@acme.example" in text and "+919820022222" in text  # addresses the visible label hides
    assert text.count("ops@acme.example") == 1  # an address already in the text is not repeated


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
def test_the_hidden_tail_is_read_only_once_expanded_and_the_toggle_is_never_text(page: Page, tier: str) -> None:
    tail = '<span hidden> The tail holds the contact: tail@acme.example</span>'
    more = '<button type="button" data-testid="expandable-text-button"><span>…</span><span>more</span></button>'
    html = BUILDERS[tier]("kT1", "Tia Ray", "tia-ray", "CEO", "5h", f"Head of the text.{tail}{more}")
    page.set_content(page_of(html))
    collapsed = parse_cards(extract_raw_cards(page), BASE, TODAY).posts[0].text
    assert collapsed == "Head of the text."
    page.evaluate("() => { document.querySelector('[hidden]').hidden = false; document.querySelector('[data-testid=\"expandable-text-button\"]').remove(); }")
    expanded = parse_cards(extract_raw_cards(page), BASE, TODAY).posts[0].text
    assert expanded.startswith("Head of the text.") and "tail@acme.example" in expanded and "more" not in expanded.split()[-1]


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
def test_non_english_and_emoji_text_survive(page: Page, tier: str) -> None:
    body = "हमें AI प्रशिक्षण चाहिए 🤖 — संपर्क: hr@zentrix.example<br>दूसरी पंक्ति"
    post = parse(page, BUILDERS[tier]("kH1", "Arjun Rao", "arjun-rao", "CTO", "4d", body)).posts[0]
    assert "प्रशिक्षण" in post.text and "🤖" in post.text and "\n" in post.text and "hr@zentrix.example" in post.text


def test_a_reshare_keeps_the_commentary_and_never_becomes_a_second_post(page: Page) -> None:
    original = sdui("kO2", "Original Poster", "original-poster", "Buyer", "1w", "The reshared ORIGINAL text with orig@buyer.example")
    parsed = parse(page, sdui("kR1", "Re Sharer", "re-sharer", "Coach", "1d", "Interesting, see below!", inner=original))
    assert [p.author_name for p in parsed.posts] == ["Re Sharer"]
    # audit #13: the original follows the commentary, clearly marked (nothing in it is lost)
    assert parsed.posts[0].text == "Interesting, see below!\n\n[Reshared post] The reshared ORIGINAL text with orig@buyer.example"


def test_a_reshare_without_commentary_falls_back_to_the_original_text(page: Page) -> None:
    original = sdui("kO2", "Original Poster", "original-poster", "Buyer", "1w", "We need a Claude Code trainer: orig@buyer.example")
    html = sdui("kR1", "Re Sharer", "re-sharer", "Coach", "1d", "", inner=original)
    parsed = parse(page, html)
    assert len(parsed.posts) == 1 and parsed.posts[0].author_name == "Re Sharer"
    assert "Claude Code trainer" in parsed.posts[0].text and "orig@buyer.example" in parsed.posts[0].text
    assert parsed.field_misses.get("text", 0) == 0


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
def test_a_text_less_card_is_a_counted_miss_not_a_post(page: Page, tier: str) -> None:
    html = BUILDERS[tier]("kN1", "No Text", "no-text", "x", "1d", "", media=ARTICLE) + BUILDERS[tier](
        "kN2", "Has Text", "has-text", "y", "1d", "Real text here."
    )
    parsed = parse(page, html)
    assert [p.author_name for p in parsed.posts] == ["Has Text"] and parsed.field_misses.get("text") == 1


def test_empty_results_parse_to_nothing_without_drift(page: Page) -> None:
    page.set_content('<html><body><main><h2>No results found</h2><p>Try shortening or rephrasing your search.</p></main></body></html>')
    assert count_cards(page) == 0
    parsed = parse_cards(extract_raw_cards(page), BASE, TODAY)
    assert parsed.posts == [] and parsed.strategy == "none"
    assert not drift_detected(0, 0, no_results=True)


@pytest.mark.parametrize("tier", ["sdui", "listitem"])
def test_every_card_reports_its_menu_and_position_for_permalink_capture(page: Page, tier: str) -> None:
    card = BUILDERS[tier]
    page.set_content(page_of(card("k1", "A One", "a-1", "h", "1d", "text one") + card("k2", "B Two", "b-2", "h", "1d", "text two")))
    raws = extract_raw_cards(page)
    assert [r["card_index"] for r in raws] == [0, 1]
    assert [r["menu_label"] for r in raws] == ["Open control menu for post by A One", "Open control menu for post by B Two"]
    assert all(r["has_control_menu"] for r in raws) and [r["component_key"] for r in raws] == ["k1", "k2"]

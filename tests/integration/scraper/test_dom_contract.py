"""The selector contract (selectors.py + EXTRACT_JS/SIGNALS_JS/SEE_MORE_JS) on LinkedIn-shaped
markup variants that neither the fixture nor the saved samples serve: every ordered fallback
of every field, card discovery edge cases, see-more selection and exclusion, no-results and
logged-out markers. Real headless Chromium on ``page.set_content`` (no network at all)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from playwright.sync_api import BrowserContext, Page, sync_playwright

from sales0.scraper.detection import CONTENT_READY_JS, SIGNAL_ARGS, read_signals
from sales0.scraper.parsing import ParsedCards, parse_cards
from sales0.scraper.search import count_cards, extract_raw_cards, see_more_buttons

pytestmark = pytest.mark.browser

LIVE = "https://www.linkedin.com"
TODAY = date(2026, 10, 27)
BLOCK = "<style>.ln{display:block}</style>"


@pytest.fixture(scope="module")
def context() -> Iterator[BrowserContext]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, chromium_sandbox=True, args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        ctx = browser.new_context()
        ctx.route("**/*", lambda route: route.abort())
        try:
            yield ctx
        finally:
            browser.close()


@pytest.fixture
def page(context: BrowserContext) -> Iterator[Page]:
    p = context.new_page()
    try:
        yield p
    finally:
        p.close()


def _parse(page: Page, body: str) -> tuple[ParsedCards, list[dict]]:
    page.set_content(f"<html><head>{BLOCK}</head><body>{body}</body></html>")
    raws = extract_raw_cards(page)
    return parse_cards(raws, LIVE, TODAY, cap=15), raws


def _actor(name: str = "Some One", slug: str = "someone", headline: str = "Head of L&D at Example Co", time: str = "2d •") -> str:
    return (
        f'<a href="/in/{slug}/"><span class="ln"><span aria-hidden="true">{name}</span></span>'
        f'<span class="ln"><span aria-hidden="true">{headline}</span></span></a>'
        f'<span class="ln"><span aria-hidden="true">{time}</span></span>'
    )


def _text(text: str) -> str:
    return f'<div class="q1" dir="ltr"><span dir="ltr">{text}</span></div>'


# --------------------------------------------------------------------------- cards
def test_every_urn_prefix_on_data_urn_and_data_id_is_a_card(page: Page) -> None:
    body = "<main>" + "".join(
        f'<div class="k3" {attr}="urn:li:{kind}:{n}">{_actor()}{_text(f"Post {n} needs a GenAI trainer")}</div>'
        for n, (attr, kind) in enumerate(
            [("data-urn", "activity"), ("data-urn", "ugcPost"), ("data-urn", "share"),
             ("data-id", "activity"), ("data-id", "ugcPost"), ("data-id", "share")], start=1)
    ) + "</main>"
    parsed, _ = _parse(page, body)
    assert [p.urn for p in parsed.posts] == [
        "urn:li:activity:1", "urn:li:ugcPost:2", "urn:li:share:3", "urn:li:activity:4", "urn:li:ugcPost:5", "urn:li:share:6",
    ]
    assert parsed.parse_errors == 0 and parsed.field_misses == {}


def test_foreign_urns_repeats_and_cards_outside_main_are_not_posts(page: Page) -> None:
    body = (
        f'<aside><div data-urn="urn:li:activity:900">{_actor()}{_text("Trending, outside main")}</div></aside>'
        "<main><ul>"
        f'<li><div data-urn="urn:li:activity:1">{_actor()}{_text("First")}</div></li>'
        f'<li><div data-urn="urn:li:group:7">{_actor()}{_text("A group, not a post")}</div></li>'
        f'<li><div data-urn="urn:li:activity:1">{_actor()}{_text("First")}</div></li>'
        "</ul></main>"
    )
    parsed, raws = _parse(page, body)
    assert [p.urn for p in parsed.posts] == ["urn:li:activity:1"]
    assert len(raws) == 2 and parsed.parse_errors == 0  # the repeat is deduped, not an error
    assert count_cards(page) == 2


def test_without_main_the_whole_document_is_the_scope(page: Page) -> None:
    parsed, _ = _parse(page, f'<div data-urn="urn:li:activity:5">{_actor()}{_text("No main landmark here")}</div>')
    assert [p.urn for p in parsed.posts] == ["urn:li:activity:5"]


@pytest.mark.parametrize(
    "container",
    ['<li class="z">{}</li>', '<article class="z">{}</article>', '<div role="article">{}</div>',
     '<div data-view-name="feed-full-update">{}</div>', '<div data-view-name="search-entity-result-universal-template">{}</div>'],
)
def test_anchor_only_cards_use_the_closest_container(page: Page, container: str) -> None:
    inner = f'{_actor()}<a href="/feed/update/urn:li:activity:77/"><span class="ln">3h •</span></a>{_text("Anchor-only post")}'
    parsed, raws = _parse(page, "<main><div>" + container.format(inner) + "</div></main>")
    assert [p.urn for p in parsed.posts] == ["urn:li:activity:77"]
    assert raws[0]["via"]["urn"] == "feed_update_href"


def test_a_post_link_without_a_container_is_skipped(page: Page) -> None:
    parsed, raws = _parse(page, f'<main><div>{_actor()}<a href="/feed/update/urn:li:activity:78/">3h</a>{_text("Loose")}</div></main>')
    assert raws == [] and parsed.posts == []


def test_a_post_link_beside_an_attribute_card_in_the_same_container_is_that_cards_link(page: Page) -> None:
    body = (
        "<main><ul><li>"
        f'<div data-urn="urn:li:activity:81">{_actor()}{_text("The post body")}</div>'
        '<a href="/feed/update/urn:li:activity:81/">Copy link</a>'
        "</li></ul></main>"
    )
    parsed, raws = _parse(page, body)
    assert [p.urn for p in parsed.posts] == ["urn:li:activity:81"] and len(raws) == 1
    assert raws[0]["via"]["urn"] == "attr"
    assert (parsed.posts[0].author_name, parsed.posts[0].text) == ("Some One", "The post body")


def test_a_nested_reshare_belongs_to_its_outer_card(page: Page) -> None:
    body = (
        '<main><li><div data-id="urn:li:activity:90">'
        f'{_actor("Outer Poster", "outer", "COO at Outer Co")}{_text("Outer commentary: we need this too")}'
        f'<div data-urn="urn:li:share:91">{_actor("Inner Poster", "inner", "Inner headline", "5d •")}'
        f'{_text("The reshared original with a much longer text that must not win the longest-text rule")}</div>'
        "</div></li></main>"
    )
    parsed, _ = _parse(page, body)
    assert len(parsed.posts) == 1
    post = parsed.posts[0]
    assert (post.urn, post.author_name, post.author_headline, post.posted_relative) == (
        "urn:li:activity:90", "Outer Poster", "COO at Outer Co", "2d")
    assert post.text == "Outer commentary: we need this too"
    assert post.author_profile_url == f"{LIVE}/in/outer/"


def test_posts_slug_links_of_every_urn_type_give_the_urn(page: Page) -> None:
    body = "<main>" + "".join(
        f'<article>{_actor()}<a href="/posts/a-b_topic-{kind}-{n}-Ab1Z/">x</a>{_text("Slug post")}</article>'
        for n, kind in ((11, "activity"), (12, "ugcPost"), (13, "share"))
    ) + "</main>"
    parsed, _ = _parse(page, body)
    assert [p.urn for p in parsed.posts] == ["urn:li:activity:11", "urn:li:ugcPost:12", "urn:li:share:13"]


# --------------------------------------------------------------------------- fields
def test_author_name_fallbacks_in_order(page: Page) -> None:
    cards = [
        # 1. [aria-hidden] inside the anchor
        '<a href="/in/a/"><span aria-hidden="true">Name Hidden</span><span>View Name Hidden’s profile</span></a>',
        # 2. span[dir] inside the anchor
        '<a href="/in/b/"><span dir="ltr">Name Dir</span></a>',
        # 3. aria-label, cleaned
        '<a href="/in/c/" aria-label="View Name Label’s profile, graphic"><img alt=""></a>',
        '<a href="/company/d/" aria-label="View Name Apostrophe\'s profile"><img alt=""></a>',
        # 4. first line of the anchor's innerText
        '<a href="/in/e/"><span class="ln">Name Line</span><span class="ln">Some headline</span></a>',
    ]
    body = "<main>" + "".join(
        f'<div data-urn="urn:li:activity:{i}">{c}{_text("Body text")}</div>' for i, c in enumerate(cards, start=1)
    ) + (
        # 5. legacy class (no profile/company anchor at all)
        '<div data-urn="urn:li:activity:6"><a class="update-components-actor__meta-link" href="/showcase/f/">'
        '<span class="update-components-actor__title"><span aria-hidden="true">Name Legacy</span></span></a>'
        f'{_text("Body text")}</div>'
    ) + "</main>"
    parsed, raws = _parse(page, body)
    assert [p.author_name for p in parsed.posts] == [
        "Name Hidden", "Name Dir", "Name Label", "Name Apostrophe", "Name Line", "Name Legacy"]
    assert [r["via"]["author_name"] for r in raws] == [
        '[aria-hidden="true"]', "span[dir]", "aria-label", "aria-label", "first_line", "legacy"]
    assert parsed.posts[5].author_profile_url == f"{LIVE}/showcase/f/"
    assert parsed.posts[3].author_profile_url == f"{LIVE}/company/d/"


def test_headline_skips_name_degree_follow_and_time_lines(page: Page) -> None:
    actor = (
        '<div class="ac"><a href="/in/x/" aria-label="View Asha Rao’s profile"><img alt=""></a>'
        '<div><a href="/in/x/"><span class="ln"><span aria-hidden="true">Asha Rao</span></span>'
        '<span class="ln">View Asha Rao’s profile</span>'
        '<span class="ln">• 2nd</span><span class="ln">2nd degree connection</span></a>'
        '<span class="ln">Premium</span><span class="ln">VP People at Kestrel Ports</span>'
        '<span class="ln">1w • Edited •</span></div><button class="ln">+ Follow</button></div>'
    )
    parsed, raws = _parse(page, f'<main><div data-urn="urn:li:activity:1">{actor}{_text("Body")}</div></main>')
    post = parsed.posts[0]
    assert (post.author_name, post.author_headline, post.posted_relative) == ("Asha Rao", "VP People at Kestrel Ports", "1w")
    assert raws[0]["via"]["author_headline"] == "author_parent"


def test_legacy_headline_and_time_are_the_last_fallbacks(page: Page) -> None:
    body = (
        '<main><div data-urn="urn:li:activity:1">'
        '<a href="/in/l/"><span aria-hidden="true">Legacy Person</span></a>'
        '<div class="update-components-actor__description"><span aria-hidden="true">Old Headline</span></div>'
        '<div class="update-components-actor__sub-description"><span aria-hidden="true">Edited</span></div>'
        f'{_text("Body")}</div></main>'
    )
    parsed, raws = _parse(page, body)
    # The anchor's parent is the card itself, so only the legacy headline applies.
    assert parsed.posts[0].author_headline == "Old Headline"
    assert raws[0]["via"]["author_headline"] == "legacy"
    assert raws[0]["via"]["posted_relative"] == "legacy"
    assert parsed.posts[0].posted_date is None  # "Edited" is not a time


def test_relative_time_fallbacks_time_element_then_own_text_then_aria_label(page: Page) -> None:
    cards = [
        f'<a href="/in/a/"><span aria-hidden="true">A</span></a><time datetime="2026-10-20T10:00:00Z"></time>{_text("t1")}',
        f'<a href="/in/b/"><span aria-hidden="true">B</span></a><span>Promoted</span><span>  3mo • </span>{_text("t2")}',
        f'<a href="/in/c/"><span aria-hidden="true">C</span></a><span aria-label="4 days ago"><svg></svg></span>{_text("t3")}',
        # a time-like string AFTER the text block is never the post time
        f'<a href="/in/d/"><span aria-hidden="true">D</span></a>{_text("t4")}<span>5h</span>',
    ]
    body = "<main>" + "".join(f'<div data-urn="urn:li:activity:{i}">{c}</div>' for i, c in enumerate(cards, 1)) + "</main>"
    parsed, raws = _parse(page, body)
    assert [p.posted_relative for p in parsed.posts] == ["2026-10-20T10:00:00Z", "3mo", "4 days ago", None]
    assert [p.posted_date for p in parsed.posts] == [date(2026, 10, 20), date(2026, 7, 29), date(2026, 10, 23), None]
    assert [r["via"].get("posted_relative") for r in raws] == ["time", "own_text", "aria-label", None]
    assert parsed.field_misses == {"author_headline": 4, "posted_relative": 1}


def test_text_is_the_longest_candidate_outside_the_author_anchor(page: Page) -> None:
    body = (
        '<main><div data-urn="urn:li:activity:1">'
        '<a href="/in/a/"><span dir="ltr">An Author Whose Name Span Is Long But Must Never Be The Post Text At All</span></a>'
        '<span dir="ltr">short</span>'
        '<div data-test-id="main-feed-activity-card__commentary">Commentary text.</div>'
        '<div dir="ltr">The real, longest post body: we need a corporate GenAI workshop.</div>'
        "</div>"
        '<div data-urn="urn:li:activity:2"><a href="/in/b/"><span aria-hidden="true">B</span></a>'
        '<div class="update-components-text">Legacy text class only</div></div>'
        '<div data-urn="urn:li:activity:3"><a href="/in/c/"><span aria-hidden="true">C</span></a></div>'
        "</main>"
    )
    parsed, raws = _parse(page, body)
    assert [p.text for p in parsed.posts] == [
        "The real, longest post body: we need a corporate GenAI workshop.", "Legacy text class only"]
    assert [r["via"].get("text") for r in raws[:2]] == ['[dir="ltr"]', ".update-components-text"]
    assert parsed.parse_errors == 1 and "no post text" in parsed.errors[0]


def test_no_class_selector_is_needed_for_obfuscated_markup(page: Page) -> None:
    body = (
        '<main class="x9Qw"><ul class="p0o1" role="list"><li class="Rk2m">'
        '<div class="t8kq2 _p3z9x" data-urn="urn:li:activity:42">'
        '<a class="Zz9" href="https://www.linkedin.com/in/obf/?miniProfileUrn=x"><span class="ln a1"><span aria-hidden="true">Obfus Cated</span></span>'
        '<span class="ln b2"><span aria-hidden="true">Head of Training at Obfusco</span></span></a>'
        '<span class="ln c3"><span aria-hidden="true">6h • </span></span>'
        '<div class="d4 _e5" dir="ltr"><span dir="ltr">Obfusco seeks an AI workshop partner.</span></div>'
        "</div></li></ul></main>"
    )
    parsed, _ = _parse(page, body)
    post = parsed.posts[0]
    assert (post.urn, post.author_name, post.author_headline, post.posted_relative, post.author_profile_url, post.text) == (
        "urn:li:activity:42", "Obfus Cated", "Head of Training at Obfusco", "6h", f"{LIVE}/in/obf/",
        "Obfusco seeks an AI workshop partner.")


# --------------------------------------------------------------------------- see more
def test_see_more_selection_order_and_exclusions(page: Page) -> None:
    cards = [
        '<button aria-label="…see more, visually reveals content">…more</button>',  # 1. aria see more
        '<button aria-label="Show more of this post">x</button>',  # 1. aria show more
        '<div role="button" aria-label="See more">x</div>',  # 1. role=button
        '<button>…see more</button>',  # 2. text
        '<span role="button">... more</span>',  # 2. text on role=button
        '<button class="feed-shared-inline-show-more-text__see-more-less-toggle">expand</button>',  # 3. legacy
        '<a href="/feed/update/x">see more</a>',  # never an a[href]
        '<button aria-label="See more options for this post">…more</button>',  # never options/menu
        '<button aria-label="Open control menu">more</button>',
        '<button aria-label="Comment">Comment</button><button>Load more comments</button>',
    ]
    body = "<main>" + "".join(
        f'<div data-urn="urn:li:activity:{i}">{_actor()}{_text("Body")}{c}</div>' for i, c in enumerate(cards, 1)
    ) + "</main>"
    page.set_content(f"<html><body>{body}</body></html>")
    buttons = see_more_buttons(page, len(cards))
    found = [b.evaluate("el => el.closest('[data-urn]').getAttribute('data-urn')") for b in buttons]
    assert found == [f"urn:li:activity:{i}" for i in range(1, 7)]
    assert len(see_more_buttons(page, 2)) == 2
    assert see_more_buttons(page, 0) == []


# --------------------------------------------------------------------------- page markers
def _signals(context: BrowserContext, page: Page, body: str) -> tuple:
    page.set_content(f"<html><body>{body}</body></html>")
    return read_signals(page, None, context, LIVE)


@pytest.mark.parametrize(
    ("body", "no_results"),
    [
        ('<main><h2 class="a1">No results found</h2></main>', True),
        ('<main><p>No results found for "zzqx"</p></main>', True),
        ("<main><h3>  No   Results </h3></main>", True),
        ('<main><div class="search-reusable-search-no-results">Nothing</div></main>', True),  # legacy
        ("<main><p>No results so far, but we keep looking</p></main>", False),
        ("<main><span>No results found</span></main>", False),  # only h1/h2/h3/p count
        ('<main><ul role="list"><li>An unknown result layout</li></ul></main>', False),
    ],
)
def test_no_results_marker_is_text_based_with_a_legacy_fallback(context: BrowserContext, page: Page, body: str, no_results: bool) -> None:
    _, extras = _signals(context, page, body)
    assert extras["no_results"] is no_results


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ('<form action="/checkpoint/lg/login-submit"><input id="username"></form>', "has_login_form"),
        ('<input name="session_key">', "has_login_form"),
        ('<div class="authwall"></div>', "has_authwall"),
        ('<section data-test-id="authwall-join-form"></section>', "has_authwall"),
        ('<div id="authwall"></div>', "has_authwall"),
        ('<iframe src="about:blank#CAPTCHA"></iframe>', "has_captcha"),
        ('<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>', "has_captcha"),
        ('<iframe src="https://client-api.arkoselabs.com/x"></iframe>', "has_captcha"),
        ('<iframe src="https://x.funcaptcha.com/fc"></iframe>', "has_captcha"),
        ('<iframe title="Captcha Challenge" src="about:blank"></iframe>', "has_captcha"),
        ('<div id="captcha-internal"></div>', "has_captcha"),
        ('<form action="/checkpoint/challenge/captcha-verify"></form>', "has_captcha"),
    ],
)
def test_logged_out_and_captcha_markers(context: BrowserContext, page: Page, body: str, field: str) -> None:
    signals, _ = _signals(context, page, body)
    flags = {"has_login_form": signals.has_login_form, "has_authwall": signals.has_authwall, "has_captcha": signals.has_captcha}
    assert flags.pop(field) is True
    assert not any(flags.values()), flags


def test_soft_hints_are_reported_by_name_and_a_bare_page_has_none(context: BrowserContext, page: Page) -> None:
    signals, _ = _signals(
        context, page,
        '<header id="global-nav" class="global-nav"></header><nav aria-label="Primary" data-test-global-nav></nav>'
        '<main><a href="/in/me/">Me</a></main>',
    )
    assert set(signals.soft_hints) == {"self_profile_link", "global_nav_attr", "nav_aria", "main_landmark", "legacy_global_nav"}
    assert signals.has_session_cookie is False  # this context has no li_at
    bare, extras = _signals(context, page, "<div>hello</div>")
    assert bare.soft_hints == () and extras == {"no_results": False, "cards_found": 0}


def test_content_ready_waits_for_cards_no_results_or_a_safety_marker(page: Page) -> None:
    for body, ready in [
        ("<main><div>loading…</div></main>", False),
        (f'<main><div data-urn="urn:li:activity:1">{_text("x")}</div></main>', True),
        ("<main><h2>No results found</h2></main>", True),
        ('<iframe src="https://client-api.arkoselabs.com/x"></iframe>', True),
        ('<form action="/login-submit"></form>', True),
    ]:
        page.set_content(f"<html><body>{body}</body></html>")
        assert page.evaluate(CONTENT_READY_JS, SIGNAL_ARGS) is ready, body

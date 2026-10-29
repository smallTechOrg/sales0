"""Fixture LinkedIn markup contract (architecture.md → Fixture markup, Phase 1.1 + Phase 1.2).

The fixture must look like CURRENT LinkedIn: obfuscated class names, no ``global-nav`` and
none of Phase 1's ``update-components-*`` / ``feed-shared-*`` / ``search-reusable-*``
classes. Phase 1.2: every results page is SDUI markup (lazy column → listitem[componentkey]
→ div[componentkey=KEY] → feed-full-update) with NO ``urn:li:``, ``/feed/update/`` or
``/posts/`` anywhere; ``legacy-urn-test`` keeps the four Phase 1.1 URN card shapes.
Rendered in-process (no browser); a tiny stdlib DOM is enough to check structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from sales0.linkedin_fixture import build_app
from sales0.linkedin_fixture.corpus import (
    DRIFT_ITEM_KEYS,
    LAZY_PLANS,
    LEGACY_KEYWORD,
    PERMALINK_MISS_KEYWORD,
    SEE_MORE_AT,
    TRIGGER_KEYWORDS,
    FixturePost,
    load_corpus,
    permalink_miss_posts,
    posts_for,
)
from sales0.scraper.urls import FIXTURE_PREFIX

P = FIXTURE_PREFIX
LEGACY = ("update-components", "feed-shared", "global-nav", "search-reusable", "reusable-search")
# Obfuscated, deterministic class tokens: 4-6 lowercase letters/digits, optionally "_"-prefixed.
OBFUSCATED_CLASS = re.compile(r"_?[a-z0-9]{4,6}")
# The one readable class the spec requires: the authwall's logged-out marker (`.authwall`).
MARKER_CLASSES = {"authwall"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
URN_ATTR_PREFIXES = ("urn:li:activity:", "urn:li:ugcPost:", "urn:li:share:")
POST_LINK = re.compile(r"/feed/update/urn:li:(activity|ugcPost|share):\d+|/posts/")
CORPUS_KEYWORDS = ("corporate ai training", "ai workshop", "ai consulting", "claude code training")
LISTITEM_KEY_RE = re.compile(r"^expanded(.+?)FeedType_[A-Za-z_]+$")  # selectors.SDUI_LISTITEM_KEY_RE
SDUI_SCREEN = "com.linkedin.sdui.flagshipnav.search.SearchResultsContent"
MENU_ITEMS = ["Save", "Copy link to post", "Not interested", "Unfollow {name}", "Report post"]
# Every keyword whose results page must be SDUI markup (all but legacy-urn-test and the
# redirect / safety pages).
SDUI_KEYWORDS = (*CORPUS_KEYWORDS, PERMALINK_MISS_KEYWORD, *LAZY_PLANS)


# ------------------------------------------------------------------------- tiny DOM
@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Node] = field(default_factory=list)
    text: str = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find_all(self, pred) -> list[Node]:
        return [n for n in self.walk() if n is not self and pred(n)]

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def all_text(self) -> str:
        return "".join(n.text for n in self.walk())


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root
        self.in_template = 0

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, parent=self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.text += data


def dom(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    return builder.root


def main_of(root: Node) -> Node:
    mains = root.find_all(lambda n: n.tag == "main")
    assert len(mains) == 1
    return mains[0]


def urn_of(node: Node) -> str | None:
    for attr in ("data-urn", "data-id"):
        value = node.attrs.get(attr, "")
        if value.startswith(URN_ATTR_PREFIXES):
            return value
    return None


# ------------------------------------------------------------------------- pages
@pytest.fixture(scope="module")
def anon() -> TestClient:
    return TestClient(build_app(), follow_redirects=False)


@pytest.fixture(scope="module")
def client() -> TestClient:
    c = TestClient(build_app(), follow_redirects=False)
    assert c.get(f"{P}/login", params={"direct": "1"}).status_code == 302
    return c


def _search(client: TestClient, keyword: str):
    return client.get(f"{P}/search/results/content/", params={"keywords": keyword, "origin": "GLOBAL_SEARCH_HEADER"})


def _rendered_pages(client: TestClient, anon: TestClient) -> dict[str, str]:
    pages: dict[str, str] = {
        "login": anon.get(f"{P}/login").text,
        "verify": anon.get(f"{P}/checkpoint/challenge/verify").text,
        "remember": client.get(f"{P}/checkpoint/challenge/remember-browser").text,
        "authwall": anon.get(f"{P}/authwall").text,
        "checkpoint": client.get(f"{P}/checkpoint/challenge/").text,
        "feed": client.get(f"{P}/feed/").text,
        "post": client.get(f"{P}/feed/update/urn:li:activity:7300000000000000001/").text,
        "post-missing": client.get(f"{P}/feed/update/urn:li:activity:1/").text,
        "no-results": _search(client, "zzqx nothing matches").text,
    }
    for keyword in CORPUS_KEYWORDS:
        pages[f"search:{keyword}"] = _search(client, keyword).text
    for keyword in TRIGGER_KEYWORDS:
        response = _search(client, keyword)
        if response.status_code != 302:
            pages[f"search:{keyword}"] = response.text
    return pages


def test_no_page_carries_legacy_classes_or_global_nav(client: TestClient, anon: TestClient) -> None:
    pages = _rendered_pages(client, anon)
    assert len(pages) >= 19
    for name, html in pages.items():
        lowered = html.lower()
        for legacy in LEGACY:
            assert legacy not in lowered, f"{name} contains {legacy!r}"


def test_every_class_token_is_obfuscated(client: TestClient, anon: TestClient) -> None:
    for name, html in _rendered_pages(client, anon).items():
        tokens = [t for n in dom(html).walk() for t in n.attrs.get("class", "").split()]
        assert tokens, f"{name} has no classes at all"
        bad = [t for t in tokens if t not in MARKER_CLASSES and not OBFUSCATED_CLASS.fullmatch(t)]
        assert not bad, f"{name} has readable class names {bad}"


def test_feed_is_current_linkedin_shaped(client: TestClient) -> None:
    root = dom(client.get(f"{P}/feed/").text)
    main_of(root)
    navs = root.find_all(lambda n: n.tag == "nav" and n.attrs.get("aria-label") == "Primary")
    assert len(navs) == 1
    assert root.find_all(lambda n: n.tag == "a" and "/in/" in n.attrs.get("href", ""))  # self-profile link
    assert not root.find_all(lambda n: "global-nav" in n.attrs.get("id", "") + n.attrs.get("class", ""))


# ------------------------------------------------------------------------- Phase 1.2: SDUI
def _view(name: str):
    return lambda n: n.attrs.get("data-view-name") == name


def _sdui_column(html: str) -> Node:
    """main > div[data-sdui-screen] > div[data-testid=lazy-column] (exactly one of each)."""
    main = main_of(dom(html))
    screens = [n for n in main.children if SDUI_SCREEN in n.attrs.get("data-sdui-screen", "")]
    assert len(screens) == 1
    columns = [n for n in screens[0].children if n.attrs.get("data-testid") == "lazy-column"]
    assert len(columns) == 1
    return columns[0]


def _sdui_posts(column: Node) -> list[tuple[str, Node]]:
    """[(KEY, feed-full-update)] in DOM order, asserting the listitem → KEY div → post nesting."""
    found = []
    for item in column.children:
        assert item.attrs.get("role") == "listitem", item.tag
        wrapper_key = item.attrs.get("componentkey", "")
        m = LISTITEM_KEY_RE.match(wrapper_key)
        assert m and wrapper_key.endswith("FeedType_FLAGSHIP_SEARCH"), wrapper_key
        (inner,) = item.children
        assert inner.tag == "div" and inner.attrs.get("componentkey") == m.group(1)
        (post,) = inner.children
        assert post.attrs.get("data-view-name") == "feed-full-update"
        assert not post.find_all(lambda n: "componentkey" in n.attrs)  # the KEY comes from the wrapper only
        found.append((m.group(1), post))
    return found


def _expected_ids(keyword: str) -> list[str]:
    if keyword == PERMALINK_MISS_KEYWORD:
        return [p.id for p in permalink_miss_posts()]
    if keyword in LAZY_PLANS:
        return [p.id for p in posts_for(LAZY_PLANS[keyword].source_keyword)][: LAZY_PLANS[keyword].initial]
    return [p.id for p in posts_for(keyword)]


@pytest.mark.parametrize("keyword", SDUI_KEYWORDS)
def test_sdui_pages_carry_no_urn_and_no_post_link_anywhere(client: TestClient, keyword: str) -> None:
    html = _search(client, keyword).text
    assert "urn:li:" not in html
    assert "/feed/update/" not in html
    assert "/posts/" not in html
    root = dom(html)
    assert not any(urn_of(n) or "data-urn" in n.attrs or "data-id" in n.attrs for n in root.walk())


@pytest.mark.parametrize("keyword", SDUI_KEYWORDS)
def test_sdui_results_nest_listitem_key_and_post_in_corpus_order(client: TestClient, keyword: str) -> None:
    by_id = {p.id: p for p in load_corpus()}
    posts = _sdui_posts(_sdui_column(_search(client, keyword).text))
    expected = [by_id[i] for i in _expected_ids(keyword)]
    assert [key for key, _ in posts] == [p.key_for(keyword) for p in expected]


def test_p1s_key_differs_between_its_two_keywords(client: TestClient) -> None:
    keys = {
        kw: dict(_sdui_posts(_sdui_column(_search(client, kw).text)))
        for kw in ("corporate ai training", "ai workshop")
    }
    p1 = next(p for p in load_corpus() if p.id == "P1")
    a, b = p1.key_for("corporate ai training"), p1.key_for("ai workshop")
    assert a != b and a in keys["corporate ai training"] and b in keys["ai workshop"]
    assert a not in keys["ai workshop"]


def _check_sdui_post(post: FixturePost, card: Node, copy_link: str) -> None:
    profile = f"/{post.profile_path}/"
    actors = card.find_all(_view("feed-actor"))
    assert len(actors) == 2
    avatar = [a for a in actors[0].find_all(lambda n: n.tag == "a")]
    assert len(avatar) == 1 and avatar[0].attrs["href"].endswith(profile)
    assert avatar[0].attrs.get("aria-label") == f"View {post.author_name}’s profile"
    assert avatar[0].all_text().strip() == ""  # image only
    links = actors[1].find_all(lambda n: n.tag == "a")
    assert len(links) == 1 and links[0].attrs["href"].endswith(profile)
    lines = [n.text.strip() for n in actors[1].walk() if n.text.strip()]
    assert lines == [post.author_name, "• 3rd+", post.author_headline, f"{post.posted_relative} •", "Follow"]
    # Text: feed-commentary > expandable-text-box (head + hidden tail) + the "…more" button.
    (commentary,) = card.find_all(_view("feed-commentary"))
    (box,) = commentary.find_all(lambda n: n.attrs.get("data-testid") == "expandable-text-box")
    assert box.all_text() == post.text
    hidden = box.find_all(lambda n: "hidden" in n.attrs)
    more = commentary.find_all(lambda n: n.attrs.get("data-testid") == "expandable-text-button")
    if len(post.text) > SEE_MORE_AT:
        assert [h.all_text() for h in hidden] == [post.text_tail]
        assert len(more) == 1 and more[0].tag == "button" and more[0].all_text() == "…more"
    else:
        assert hidden == [] and more == []
    # Control menu by copy_link.
    menus = card.find_all(_view("feed-control-menu"))
    if copy_link == "none":
        assert menus == []
        assert "Copy link to post" not in card.all_text()
        return
    (menu,) = menus
    trigger = menu.children[0]
    assert trigger.tag == "button" and trigger.attrs.get("type") == "button"
    assert trigger.attrs.get("aria-label") == f"Open control menu for post by {post.author_name}"
    assert trigger.attrs.get("aria-expanded") == "false"
    (items_box,) = menu.find_all(lambda n: n.attrs.get("role") == "menu")
    assert "hidden" in items_box.attrs
    items = [n.all_text().strip() for n in items_box.find_all(lambda n: n.attrs.get("role") == "menuitem")]
    assert items == [i.format(name=post.author_name) for i in MENU_ITEMS]


@pytest.mark.parametrize("keyword", CORPUS_KEYWORDS)
def test_sdui_post_blocks_follow_the_spec_template(client: TestClient, keyword: str) -> None:
    by_key = {p.key_for(keyword): p for p in posts_for(keyword)}
    for key, card in _sdui_posts(_sdui_column(_search(client, keyword).text)):
        post = by_key[key]
        _check_sdui_post(post, card, post.copy_link)


def test_copy_link_variants_cover_urn_noop_and_none_across_the_corpus(client: TestClient) -> None:
    seen: dict[str, set[str]] = {"urn": set(), "noop": set(), "none": set()}
    for keyword in CORPUS_KEYWORDS:
        html = _search(client, keyword).text
        by_key = {p.key_for(keyword): p for p in posts_for(keyword)}
        for key, card in _sdui_posts(_sdui_column(html)):
            post = by_key[key]
            seen[post.copy_link].add(post.id)
            has_menu = bool(card.find_all(_view("feed-control-menu")))
            assert has_menu == (post.copy_link != "none")
            # The share slug reaches the page only inside the menu script, and only for "urn" posts.
            assert (post.posts_slug in html) == (post.copy_link == "urn"), post.id
    assert seen == {"urn": {"P2", "P5", "P8", "P10"}, "noop": {"P1", "P6", "P9"}, "none": {"P3", "P7", "P11"}}


def test_company_author_links_to_a_company_page(client: TestClient) -> None:
    html = _search(client, "claude code training").text
    assert "/company/briltus-academy/" in html and "/in/briltus-academy/" not in html


def test_permalink_miss_page_renders_p1_p6_p9_p2_with_every_menu_a_noop(client: TestClient) -> None:
    html = _search(client, PERMALINK_MISS_KEYWORD).text
    posts = _sdui_posts(_sdui_column(html))
    expected = permalink_miss_posts()
    assert [k for k, _ in posts] == [p.key_for(PERMALINK_MISS_KEYWORD) for p in expected]
    for post, (_key, card) in zip(expected, posts, strict=True):
        _check_sdui_post(post, card, "noop")
        assert post.posts_slug not in html  # nothing can be copied, even for P2
    assert "Copy link to post" in html


def test_layout_drift_page_has_sdui_listitems_but_no_post(client: TestClient) -> None:
    html = _search(client, "layout-drift-test").text
    column = _sdui_column(html)
    items = column.children
    assert len(items) == 3
    for item, key in zip(items, DRIFT_ITEM_KEYS, strict=True):
        assert item.attrs.get("role") == "listitem"
        assert item.attrs.get("componentkey") == f"expanded{key}FeedType_FLAGSHIP_SEARCH"
        assert item.all_text().strip()
    root = dom(html)
    assert not root.find_all(_view("feed-full-update"))
    assert not any(urn_of(n) for n in root.walk())
    assert not POST_LINK.search(html) and "urn:li:" not in html
    main = main_of(root)
    headings = [n.all_text().strip().lower() for n in main.walk() if n.tag in ("h1", "h2", "h3", "p")]
    assert not any(h.startswith("no results found") or h == "no results" for h in headings)


@pytest.mark.parametrize("keyword", sorted(LAZY_PLANS))
def test_lazy_pages_template_the_remaining_posts_as_sdui_listitems(client: TestClient, keyword: str) -> None:
    html = _search(client, keyword).text
    plan = LAZY_PLANS[keyword]
    source = posts_for(plan.source_keyword)
    templates = re.findall(r"<template[^>]*>(.*?)</template>", html, flags=re.S)
    assert len(templates) == len(source) - plan.initial
    for post, fragment in zip(source[plan.initial :], templates, strict=True):
        posts = _sdui_posts(Node("#col", {}, children=dom(fragment).children))
        assert [k for k, _ in posts] == [post.key_for(keyword)]
    assert 'data-testid="lazy-column"' in html


def test_sdui_text_is_escaped_never_rendered_as_html(client: TestClient) -> None:
    html = _search(client, "corporate ai training").text
    assert "Head of L&amp;D at Acme Logistics" in html  # autoescaped


# ------------------------------------------------------------------------- legacy-urn-test (Phase 1.1 shapes)
def _cards(html: str) -> dict[str, tuple[str, Node]]:
    """URN → (shape, card element), discovered the way the spec's URN strategy does."""
    main = main_of(dom(html))
    found: dict[str, tuple[str, Node]] = {}
    attr_cards = [n for n in main.walk() if urn_of(n)]
    outer = [n for n in attr_cards if not any(a in attr_cards for a in n.ancestors())]
    for card in outer:
        shape = "data-urn" if "data-urn" in card.attrs else "data-id"
        if shape == "data-id":
            assert card.tag == "div" and card.parent is not None and card.parent.tag == "li"
        else:
            assert card.tag == "div"
        found[urn_of(card)] = (shape, card)
    for anchor in main.find_all(lambda n: n.tag == "a" and POST_LINK.search(n.attrs.get("href", ""))):
        if any(a in outer for a in anchor.ancestors()):
            continue
        container = next(a for a in anchor.ancestors() if a.tag in ("li", "article"))
        href = anchor.attrs["href"]
        if m := re.search(r"/feed/update/(urn:li:activity:\d+)/", href):
            assert container.tag == "li"
            found[m.group(1)] = ("update-link", container)
        else:
            m = re.search(r"/posts/[^/]*-(activity)-(\d+)-AbCd/", href)
            assert m and container.tag == "article"
            found[f"urn:li:{m.group(1)}:{m.group(2)}"] = ("posts-link", container)
        assert urn_of(container) is None
        assert not any(urn_of(n) for n in container.walk())
    return found


def test_legacy_urn_test_renders_every_post_in_its_phase_1_1_card_shape(client: TestClient) -> None:
    html = _search(client, LEGACY_KEYWORD).text
    cards = _cards(html)
    assert {urn: shape for urn, (shape, _) in cards.items()} == {p.urn: p.shape for p in load_corpus()}
    assert len(cards) == 10
    assert {shape for shape, _ in cards.values()} == {"data-urn", "data-id", "update-link", "posts-link"}
    root = dom(html)
    assert not root.find_all(_view("feed-full-update")) and not root.find_all(_view("feed-control-menu"))
    assert 'componentkey' not in html


def test_legacy_author_block_text_and_see_more_have_no_actor_classes(client: TestClient) -> None:
    by_urn = {p.urn: p for p in load_corpus()}
    for urn, (_shape, card) in _cards(_search(client, LEGACY_KEYWORD).text).items():
        post = by_urn[urn]
        authors = card.find_all(lambda n: n.tag == "a" and f"/{post.profile_path}/" in n.attrs.get("href", ""))
        assert len(authors) == 1, urn
        author = authors[0]
        names = author.find_all(lambda n: n.tag == "span" and n.attrs.get("aria-hidden") == "true")
        assert [n.all_text().strip() for n in names] == [post.author_name]
        lines = [n.text.strip() for n in author.walk() if n.text.strip()]  # each element's own text
        assert lines == [post.author_name, post.author_headline, f"{post.posted_relative} •"], urn
        texts = card.find_all(lambda n: n.tag in ("div", "span") and n.attrs.get("dir") == "ltr")
        assert len(texts) == 1 and texts[0].all_text() == post.text  # head + hidden tail
        hidden = texts[0].find_all(lambda n: "hidden" in n.attrs)
        buttons = card.find_all(lambda n: n.tag == "button")
        if len(post.text) > SEE_MORE_AT:
            assert [h.all_text() for h in hidden] == [post.text_tail]
            assert len(buttons) == 1
            label = buttons[0].attrs.get("aria-label", "")
            assert label == "…see more, visually reveals content which is already detected by screen readers"
            assert buttons[0].attrs.get("type") == "button"
        else:
            assert hidden == [] and buttons == []


# ------------------------------------------------------------------------- other pages
def test_no_results_is_a_text_marker_only(client: TestClient) -> None:
    html = _search(client, "zzqx nothing matches").text
    main = main_of(dom(html))
    headings = [n.all_text().strip() for n in main.walk() if n.tag in ("h1", "h2", "h3", "p")]
    assert any(h.lower().startswith("no results found") for h in headings)
    assert not any(urn_of(n) for n in main.walk())
    assert not POST_LINK.search(html)
    assert not main.find_all(lambda n: n.attrs.get("role") == "listitem")  # no drift container either


def test_login_and_authwall_carry_the_logged_out_markers(client: TestClient, anon: TestClient) -> None:
    login = dom(anon.get(f"{P}/login").text)
    assert login.find_all(lambda n: n.tag == "input" and n.attrs.get("id") == "username")
    assert login.find_all(lambda n: n.tag == "form" and "login-submit" in n.attrs.get("action", ""))
    wall = dom(anon.get(f"{P}/authwall").text)
    assert wall.find_all(lambda n: "authwall" in n.attrs.get("class", "").split())
    assert wall.find_all(lambda n: n.tag == "form" and "login" in n.attrs.get("action", ""))

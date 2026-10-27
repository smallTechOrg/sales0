"""Fixture LinkedIn markup contract (architecture.md → Fixture markup, Phase 1.1).

The fixture must look like CURRENT LinkedIn: obfuscated class names, no ``global-nav`` and
none of Phase 1's ``update-components-*`` / ``feed-shared-*`` / ``search-reusable-*``
classes, and the four card shapes the parser's fallbacks must handle. Rendered in-process
(no browser); a tiny stdlib DOM is enough to check structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from sales0.linkedin_fixture import build_app
from sales0.linkedin_fixture.corpus import SEE_MORE_AT, TRIGGER_KEYWORDS, load_corpus, posts_for
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
    for keyword in ("corporate ai training", "ai workshop", "ai consulting", "claude code training"):
        pages[f"search:{keyword}"] = _search(client, keyword).text
    for keyword in TRIGGER_KEYWORDS:
        response = _search(client, keyword)
        if response.status_code != 302:
            pages[f"search:{keyword}"] = response.text
    return pages


def test_no_page_carries_legacy_classes_or_global_nav(client: TestClient, anon: TestClient) -> None:
    pages = _rendered_pages(client, anon)
    assert len(pages) >= 17
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


def _cards(html: str) -> dict[str, tuple[str, Node]]:
    """URN → (shape, card element), discovered the way the spec's EXTRACT_JS does."""
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


@pytest.mark.parametrize("keyword", ["corporate ai training", "ai workshop", "ai consulting", "claude code training"])
def test_results_use_the_specified_card_shape_per_post(client: TestClient, keyword: str) -> None:
    cards = _cards(_search(client, keyword).text)
    expected = {p.urn: p.shape for p in posts_for(keyword)}
    assert {urn: shape for urn, (shape, _) in cards.items()} == expected
    assert len(cards) == len(posts_for(keyword))  # Phase 1 counts: 3 / 4 / 2 / 2


def test_all_four_card_shapes_appear_across_the_corpus(client: TestClient) -> None:
    shapes = set()
    for keyword in ("corporate ai training", "ai workshop", "ai consulting", "claude code training"):
        shapes |= {shape for shape, _ in _cards(_search(client, keyword).text).values()}
    assert shapes == {"data-urn", "data-id", "update-link", "posts-link"}


def test_author_block_text_and_see_more_have_no_actor_classes(client: TestClient) -> None:
    by_urn = {p.urn: p for p in load_corpus()}
    for keyword in ("corporate ai training", "ai workshop", "ai consulting", "claude code training"):
        for urn, (_shape, card) in _cards(_search(client, keyword).text).items():
            post = by_urn[urn]
            authors = card.find_all(lambda n: n.tag == "a" and f"/in/{post.profile_slug}/" in n.attrs.get("href", ""))
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


def test_no_results_is_a_text_marker_only(client: TestClient) -> None:
    html = _search(client, "zzqx nothing matches").text
    main = main_of(dom(html))
    headings = [n.all_text().strip() for n in main.walk() if n.tag in ("h1", "h2", "h3", "p")]
    assert any(h.lower().startswith("no results found") for h in headings)
    assert not any(urn_of(n) for n in main.walk())
    assert not POST_LINK.search(html)


def test_layout_drift_page_has_list_items_without_urns_or_post_links(client: TestClient) -> None:
    html = _search(client, "layout-drift-test").text
    main = main_of(dom(html))
    lists = [n for n in main.children if n.tag == "ul" and n.attrs.get("role") == "list"]
    assert len(lists) == 1
    items = [n for n in lists[0].children if n.tag == "li"]
    assert len(items) >= 2 and all(i.all_text().strip() for i in items)
    assert not any(urn_of(n) for n in main.walk())
    assert not POST_LINK.search(html)
    headings = [n.all_text().strip().lower() for n in main.walk() if n.tag in ("h1", "h2", "h3", "p")]
    assert not any(h.startswith("no results found") or h == "no results" for h in headings)


def test_login_and_authwall_carry_the_logged_out_markers(client: TestClient, anon: TestClient) -> None:
    login = dom(anon.get(f"{P}/login").text)
    assert login.find_all(lambda n: n.tag == "input" and n.attrs.get("id") == "username")
    assert login.find_all(lambda n: n.tag == "form" and "login-submit" in n.attrs.get("action", ""))
    wall = dom(anon.get(f"{P}/authwall").text)
    assert wall.find_all(lambda n: "authwall" in n.attrs.get("class", "").split())
    assert wall.find_all(lambda n: n.tag == "form" and "login" in n.attrs.get("action", ""))

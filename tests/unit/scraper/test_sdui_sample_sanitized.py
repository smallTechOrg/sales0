"""The saved SDUI sample is sanitised: synthetic data only, real structure kept.

architecture.md → "## LinkedIn SDUI search results (Phase 1.2)" → Saved SDUI sample. Pure: the file is read as
text and walked with the stdlib ``html.parser``; nothing is rendered and nothing touches the network. Every check
is a small function returning its violations, so the same checks are proven to BITE on crafted leaky snippets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "linkedin_pages"
SAMPLE = PAGES / "search_results_sdui.html"
MANIFEST = json.loads((PAGES / "manifest.json").read_text(encoding="utf-8"))
CASE = next(c for c in MANIFEST["cases"] if c["id"] == "search_results_sdui")
SYNTHETIC = CASE["synthetic"]
EXPECT = CASE["expect"]

ALLOWED_ATTRS = {"class", "id", "role", "componentkey", "href", "src", "alt", "dir", "hidden", "type", "tabindex",
                 "data-view-name", "data-testid", "data-sdui-screen"}
FORBIDDEN_MARKERS = ("<script", "<iframe", "<noscript", "<style", "<link", "licdn.com", "lnkd.in",
                     "urn:li:member", "urn:li:fsd_profile", "trackingid", "trk=", "urn:li:")
MEMBER_ID_PREFIX = "ACoA"  # case-sensitive: synthetic random keys may contain "acoa" in other cases
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
WRAPPER_KEY_RE = re.compile(r"^(?:expanded|update-card-focus)(.+?)FeedType_FLAGSHIP_SEARCH$")
DEGREE_RE = re.compile(r"^•?\s*(1st|2nd|3rd\+?)$")
TIME_RE = re.compile(r"^\d+\s?(s|m|h|d|w|mo|yr)\b(\s*•\s*Edited)?$")
EXAMPLE_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.example\b")
PROFILE_HREF_RE = re.compile(r"/(in|company)/([^/?#\"'\s]+)")
MAX_BYTES = 300 * 1024


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None
    children: list[Node | str] = field(default_factory=list)

    def ancestors_or_self(self):
        n: Node | None = self
        while n is not None:
            yield n
            n = n.parent

    def text_runs(self) -> list[str]:
        out: list[str] = []
        for c in self.children:
            out.extend([c] if isinstance(c, str) else c.text_runs())
        return out

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        for n in self.cur.ancestors_or_self():
            if n.tag == tag and n.parent is not None:
                self.cur = n.parent
                return

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


def nodes(root: Node) -> list[Node]:
    return [n for n in root.walk() if n is not root]


def by_view(root: Node, name: str) -> list[Node]:
    return [n for n in nodes(root) if n.attrs.get("data-view-name") == name]


def expanded_labels(syn: dict) -> set[str]:
    out: set[str] = set()
    for label in syn["labels"]:
        if "{name}" in label:
            out |= {label.replace("{name}", n) for n in syn["names"]}
        else:
            out.add(label)
    return out


# ------------------------------------------------------------------------------------------------ checks


def check_markers(html: str, syn: dict) -> list[str]:
    low = html.lower()
    bad = [f"forbidden marker {m!r}" for m in FORBIDDEN_MARKERS if m in low]
    if MEMBER_ID_PREFIX in html:
        bad.append(f"forbidden marker {MEMBER_ID_PREFIX!r}")
    if len(html.encode("utf-8")) >= MAX_BYTES:
        bad.append("file is not < 300 KB")
    return bad


def check_emails(html: str, syn: dict) -> list[str]:
    stripped = EXAMPLE_EMAIL_RE.sub("", html)
    return ["'@' outside a .example address"] if "@" in stripped else []


def check_attributes(html: str, syn: dict) -> list[str]:
    bad = set()
    for n in nodes(parse(html)):
        for k in n.attrs:
            if k not in ALLOWED_ATTRS and not k.startswith("aria-"):
                bad.add(f"attribute outside the allowlist: {k} on <{n.tag}>")
            if k.startswith("on"):
                bad.add(f"inline event handler {k}")
    return sorted(bad)


def check_slugs(html: str, syn: dict) -> list[str]:
    allowed = set(syn["slugs"])
    found = {f"{kind}/{slug}".lower() for kind, slug in PROFILE_HREF_RE.findall(html)}
    return [f"slug not synthetic: {s}" for s in sorted(found - allowed)]


def check_links_and_media(html: str, syn: dict) -> list[str]:
    bad = []
    for n in nodes(parse(html)):
        href = n.attrs.get("href")
        if href is not None and href != "#" and not re.match(r"^https://www\.linkedin\.com/(in|company|search)/", href):
            bad.append(f"href not '#', /in/, /company/ or /search/: <{n.tag}>")
        src = n.attrs.get("src")
        if src is not None and not (src.startswith("data:") or src.startswith("https://example.invalid/")):
            bad.append(f"src is not a placeholder: <{n.tag}>")
    return bad


def check_labels(html: str, syn: dict) -> list[str]:
    allowed = expanded_labels(syn)
    bad = set()
    for n in nodes(parse(html)):
        for k in ("aria-label", "alt"):
            v = n.attrs.get(k)
            if v is not None and v not in allowed:
                bad.add(f"{k} not a synthetic label: {v!r}")
    return sorted(bad)


def check_componentkeys(html: str, syn: dict) -> list[str]:
    keys = set(syn["keys"])
    bad = set()
    for n in nodes(parse(html)):
        v = n.attrs.get("componentkey")
        if v is None:
            continue
        m = WRAPPER_KEY_RE.match(v)
        if (m.group(1) if m else v) not in keys:
            bad.add(f"componentkey not synthetic: {v!r}")
    return sorted(bad)


def check_actor_names(html: str, syn: dict, headlines: set[str]) -> list[str]:
    names = set(syn["names"])
    bad = set()
    for actor in by_view(parse(html), "feed-actor"):
        for line in (r.strip() for r in actor.text_runs()):
            if not line or line == "Follow" or DEGREE_RE.match(line) or TIME_RE.match(line) or line in headlines:
                continue
            if line not in names:
                bad.add(f"feed-actor line is not a synthetic name: {line!r}")
    return sorted(bad)


def check_structure(html: str, syn: dict) -> list[str]:
    root = parse(html)
    bad = []
    posts = by_view(root, "feed-full-update")
    if len(posts) != 6:
        bad.append(f"expected exactly 6 feed-full-update, found {len(posts)}")
    menus = by_view(root, "feed-control-menu")
    if not any("Copy link to post" in "".join(m.text_runs()) for m in menus):
        bad.append("no feed-control-menu contains 'Copy link to post'")
    return bad


SAMPLE_CHECKS = [check_markers, check_emails, check_attributes, check_slugs, check_links_and_media, check_labels,
                 check_componentkeys, check_structure]
HEADLINES = {f["author_headline"] for f in EXPECT["fields"].values() if f["author_headline"]}


@pytest.fixture(scope="module")
def html() -> str:
    return SAMPLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(html: str) -> Node:
    return parse(html)


# ------------------------------------------------------------------------------------------------ happy path


@pytest.mark.parametrize("check", SAMPLE_CHECKS, ids=[c.__name__ for c in SAMPLE_CHECKS])
def test_the_sample_passes_every_sanitisation_check(html: str, check) -> None:
    assert check(html, SYNTHETIC) == []


def test_every_feed_actor_line_is_a_synthetic_name_degree_time_or_follow(html: str) -> None:
    assert check_actor_names(html, SYNTHETIC, HEADLINES) == []


def test_pinned_authors_are_synthetic_and_present(html: str) -> None:
    slugs = set(SYNTHETIC["slugs"])
    for ref, want in EXPECT["fields"].items():
        assert want["author_name"] in SYNTHETIC["names"], ref
        kind, slug = PROFILE_HREF_RE.search(want["author_profile_url"]).groups()
        assert f"{kind}/{slug}" in slugs, ref
        assert f'href="{want["author_profile_url"]}"' in html, ref
        assert want["posted_relative"] in html and want["text_contains"] in html, ref
        if want["author_headline"]:
            assert want["author_headline"].replace("&", "&amp;") in html, ref


def test_refs_are_the_inner_component_keys_of_the_six_posts_in_dom_order(tree: Node) -> None:
    refs = []
    for post in by_view(tree, "feed-full-update"):
        inner = next(n for n in post.walk() if n is not post and "componentkey" in n.attrs)
        refs.append("ck:" + inner.attrs["componentkey"])
        wrapper = next(a for a in post.ancestors_or_self() if a.attrs.get("role") == "listitem")
        m = WRAPPER_KEY_RE.match(wrapper.attrs["componentkey"])
        assert m and m.group(1) == inner.attrs["componentkey"], "listitem wrapper key embeds the post KEY"
    assert refs == EXPECT["refs"]
    assert all(r[3:] in SYNTHETIC["keys"] and len(r) <= 200 for r in refs)
    assert EXPECT["link_kinds"] == {r: "profile" for r in refs}
    assert EXPECT["strategy"] == "sdui" and EXPECT["posts"] == 6


def test_the_sdui_anchors_survive_sanitisation(tree: Node, html: str) -> None:
    all_nodes = nodes(tree)
    assert sum(1 for n in all_nodes if n.attrs.get("role") == "listitem" and "componentkey" in n.attrs) == 6
    assert any(n.attrs.get("data-testid") == "lazy-column" for n in all_nodes)
    assert any("SearchResultsContent" in n.attrs.get("data-sdui-screen", "") for n in all_nodes)
    assert sum(1 for n in all_nodes if n.attrs.get("data-testid") == "expandable-text-box") == 6
    assert sum(1 for n in all_nodes if n.attrs.get("data-testid") == "expandable-text-button") == 6
    assert len(by_view(tree, "feed-actor")) == 12 and len(by_view(tree, "feed-commentary")) >= 6
    h2_texts = ["".join(n.text_runs()).strip() for n in all_nodes if n.tag == "h2"]
    assert h2_texts.count("Feed post") == 6, "the visually-hidden 'Feed post' heading is kept on every post"
    assert html.count("• 3rd+") == 5 and html.count(">Follow<") == 6
    assert any(n.tag == "main" for n in all_nodes)


def test_phones_are_synthetic(html: str) -> None:
    phones = re.findall(r"\+\d[\d ]{8,}\d", html)
    assert phones and all(re.fullmatch(r"\+91 90000 0000\d", p) for p in phones)


# ------------------------------------------------------------------------------------------------ edge cases


def test_an_empty_document_fails_the_structure_check_and_nothing_else() -> None:
    assert check_structure("", SYNTHETIC) == [
        "expected exactly 6 feed-full-update, found 0",
        "no feed-control-menu contains 'Copy link to post'",
    ]
    for check in (check_markers, check_emails, check_attributes, check_slugs, check_labels, check_componentkeys):
        assert check("", SYNTHETIC) == []


def test_name_templates_expand_over_every_synthetic_name() -> None:
    labels = expanded_labels(SYNTHETIC)
    for name in SYNTHETIC["names"]:
        assert f"Open control menu for post by {name}" in labels
        assert f"Follow {name}" in labels
    assert not any("{name}" in label for label in labels)


def test_wrapper_keys_accept_both_capture_shapes_but_only_with_a_synthetic_key() -> None:
    key = SYNTHETIC["keys"][0]
    for v in (key, f"expanded{key}FeedType_FLAGSHIP_SEARCH", f"update-card-focus{key}FeedType_FLAGSHIP_SEARCH"):
        assert check_componentkeys(f'<div componentkey="{v}"></div>', SYNTHETIC) == []
    assert check_componentkeys('<div componentkey="expandedZZnotakeyFeedType_FLAGSHIP_SEARCH"></div>', SYNTHETIC)


# ------------------------------------------------------------------------------------------------ the checks bite

LEAKS = [
    (check_markers, '<script>window.x=1</script>'),
    (check_markers, '<img src="https://media.licdn.com/dms/image/v2/x.png">'),
    (check_markers, '<a href="https://www.linkedin.com/in/x/?trk=feed">x</a>'),
    (check_markers, '<div data-x="urn:li:fsd_profile:ACoAAB1234"></div>'),
    (check_markers, "<iframe src='about:blank'></iframe>"),
    (check_emails, "<p>mail real.person@gmail.com</p>"),
    (check_attributes, '<div data-tracking-id="abc">x</div>'),
    (check_attributes, '<button onclick="go()">x</button>'),
    (check_attributes, '<img srcset="a.png 1x">'),
    (check_slugs, '<a href="https://www.linkedin.com/in/some-real-person-1234/">x</a>'),
    (check_slugs, '<a href="https://www.linkedin.com/company/real-company/">x</a>'),
    (check_links_and_media, '<a href="https://www.linkedin.com/safety/go/?url=x">x</a>'),
    (check_links_and_media, '<img src="https://cdn.example.com/face.jpg">'),
    (check_labels, '<button aria-label="Follow Real Person">Follow</button>'),
    (check_labels, '<img alt="Photo of Real Person" src="data:,">'),
    (check_componentkeys, '<div componentkey="11111111-2222-4333-8444-555555555555"></div>'),
]


@pytest.mark.parametrize("check,snippet", LEAKS, ids=[f"{c.__name__}-{i}" for i, (c, _) in enumerate(LEAKS)])
def test_each_check_flags_a_crafted_leak(check, snippet: str) -> None:
    assert check(snippet, SYNTHETIC), f"{check.__name__} must flag: {snippet}"


def test_actor_check_flags_a_real_looking_name_inside_feed_actor() -> None:
    snippet = '<a data-view-name="feed-actor" href="#"><p><span>Real Person</span></p><span>• 3rd+</span></a>'
    assert check_actor_names(snippet, SYNTHETIC, HEADLINES) == ["feed-actor line is not a synthetic name: 'Real Person'"]

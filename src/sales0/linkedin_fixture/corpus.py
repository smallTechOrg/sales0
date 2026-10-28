"""The fixture post corpus (``posts.json``; architecture.md → Fixture LinkedIn corpus).

Phase 1.2 (S4): results pages render in LinkedIn's SDUI markup (no URN anywhere in the DOM),
so each post also carries a synthetic ``component_key`` and a ``copy_link`` behaviour for its
"…" menu. The URNs stay the corpus identity; ``legacy-urn-test`` still renders the Phase 1.1
URN card shapes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

CORPUS_PATH = Path(__file__).with_name("posts.json")
# Posts longer than this render with a collapsed tail and a "see more" toggle, like LinkedIn.
SEE_MORE_AT = 220

# Keywords that trigger the safety/edge pages instead of results.
CHECKPOINT_KEYWORD = "checkpoint-test"
CAPTCHA_KEYWORD = "captcha-test"
RATELIMIT_KEYWORD = "ratelimit-test"
DRIFT_KEYWORD = "layout-drift-test"
# Phase 1.1: a session that expired mid-run (the cookie is still there; the page 302s to /authwall).
LOGOUT_KEYWORD = "logout-test"

# Lazy-loading result pages (results arrive on scroll, like LinkedIn's infinite list).
# Each reuses the posts of an existing corpus keyword, so P1-P11 stay untouched.
LAZY_KEYWORD = "lazy-scroll-test"  # "ai workshop" posts, one more revealed ~700ms after each scroll
CHECKPOINT_SCROLL_KEYWORD = "checkpoint-scroll-test"  # 1st scroll loads more; 2nd scroll's page load -> checkpoint
RATELIMIT_SCROLL_KEYWORD = "ratelimit-scroll-test"  # page loads fine; its background voyager fetch gets 429
LAZY_REVEAL_DELAY_MS = 700  # longer than the scraper's old fixed 500ms post-scroll wait

# Phase 1.2 trigger keywords.
LEGACY_KEYWORD = "legacy-urn-test"  # ALL corpus posts in their Phase 1.1 URN card shapes (strategy=urn)
PERMALINK_MISS_KEYWORD = "permalink-miss-test"  # these posts, in SDUI markup, every menu forced to "noop"
PERMALINK_MISS_IDS: tuple[str, ...] = ("P1", "P6", "P9", "P2")


@dataclass(frozen=True)
class LazyPlan:
    source_keyword: str  # whose corpus posts are served
    initial: int  # cards in the first page load
    checkpoint_at_scroll: int | None = None  # this scroll navigates to page 2, which is a checkpoint
    background_rate_limit: bool = False  # fire a voyager fetch (429) on load and on every scroll


LAZY_PLANS: dict[str, LazyPlan] = {
    LAZY_KEYWORD: LazyPlan(source_keyword="ai workshop", initial=1),
    CHECKPOINT_SCROLL_KEYWORD: LazyPlan(source_keyword="ai consulting", initial=1, checkpoint_at_scroll=2),
    RATELIMIT_SCROLL_KEYWORD: LazyPlan(source_keyword="ai consulting", initial=1, background_rate_limit=True),
}


TRIGGER_KEYWORDS: frozenset[str] = frozenset(
    {
        CHECKPOINT_KEYWORD,
        CAPTCHA_KEYWORD,
        RATELIMIT_KEYWORD,
        LOGOUT_KEYWORD,
        DRIFT_KEYWORD,
        LEGACY_KEYWORD,
        PERMALINK_MISS_KEYWORD,
        *LAZY_PLANS,
    }
)

# Phase 1.2: what the post's "…" menu "Copy link to post" item does (architecture.md → Fixture
# markup (Phase 1.2)): "urn" writes the share link (a /posts/ URL carrying the URN) through
# navigator.clipboard.writeText; "noop" only closes the menu; "none" means the post has no menu.
CopyLink = Literal["urn", "noop", "none"]
COPY_LINK_MODES: frozenset[str] = frozenset({"urn", "noop", "none"})
# The tracking query LinkedIn appends to a copied share link (never stored by sales0).
COPY_LINK_QUERY = "utm_source=share&utm_medium=member_desktop&rcm=ACoAAFixture"
# Posts whose SDUI key differs per search keyword (simulates an unstable componentkey).
UNSTABLE_KEY_IDS: frozenset[str] = frozenset({"P1"})
# The wrapper ``componentkey`` LinkedIn puts on each result listitem around the inner KEY.
LISTITEM_KEY_PREFIX = "expanded"
LISTITEM_KEY_SUFFIX = "FeedType_FLAGSHIP_SEARCH"
# Synthetic keys of the layout-drift page's listitems (no post inside them).
DRIFT_ITEM_KEYS: tuple[str, ...] = ("Dz1qWm4TyH7bNc", "Dz2rXn5UzJ8cPd", "Dz3sYo6VaK9dQe")
MAX_KEY_LEN = 197  # "ck:" + KEY must fit enquiry_sources.source_ref String(200)


def keyword_slug(keyword: str | None) -> str:
    """``"Corporate AI training"`` → ``"corporate-ai-training"`` (lower-case alphanumerics joined by '-')."""
    return re.sub(r"[^a-z0-9]+", "-", normalize_keyword(keyword)).strip("-")


def listitem_key(key: str) -> str:
    """The wrapper listitem's ``componentkey`` for inner KEY ``key``."""
    return f"{LISTITEM_KEY_PREFIX}{key}{LISTITEM_KEY_SUFFIX}"

# Phase 1.1 card shapes (architecture.md → Fixture markup), assigned by corpus post id so the
# Phase-1 counts hold through every fallback path of the parser.
CardShape = Literal["data-urn", "data-id", "update-link", "posts-link"]
CARD_SHAPES: dict[str, CardShape] = {
    "P1": "data-urn",
    "P2": "data-urn",
    "P3": "data-urn",
    "P5": "data-id",
    "P6": "data-id",
    "P7": "data-id",
    "P8": "update-link",
    "P9": "update-link",
    "P10": "posts-link",
    "P11": "posts-link",
}


@dataclass(frozen=True)
class FixturePost:
    id: str
    urn: str
    keywords: tuple[str, ...]
    author_name: str
    author_headline: str
    profile_slug: str
    posted_relative: str
    text: str
    component_key: str
    copy_link: CopyLink
    profile_kind: Literal["in", "company"] = "in"
    # Phase 1.4 (bulk corpus only): what the post is, the verdict a correct triage gives, and its age.
    kind: str = ""
    expected: Literal["genuine", "discard"] | None = None
    age_days: int = 0

    @property
    def shape(self) -> CardShape:
        return CARD_SHAPES[self.id]

    @property
    def activity_id(self) -> str:
        """The numeric part of the URN (``urn:li:activity:<n>``)."""
        return self.urn.rsplit(":", 1)[-1]

    @property
    def posts_slug(self) -> str:
        """The ``/posts/<slug>/`` permalink segment LinkedIn uses: ``<profile>_<words>-activity-<n>-<hash>``."""
        return f"{self.profile_slug}_update-activity-{self.activity_id}-AbCd"

    @property
    def profile_path(self) -> str:
        """``in/<slug>`` for people, ``company/<slug>`` for company authors (P11)."""
        return f"{self.profile_kind}/{self.profile_slug}"

    def share_url(self, base: str) -> str:
        """What "Copy link to post" writes for ``copy_link == "urn"``: a same-site /posts/ link + tracking."""
        return f"{base}/posts/{self.posts_slug}?{COPY_LINK_QUERY}"

    def key_for(self, keyword: str | None) -> str:
        """The SDUI KEY this post renders with on the results page for ``keyword``.

        Stable for every post except ``UNSTABLE_KEY_IDS`` (P1), whose KEY gains the keyword
        slug, so the same post carries a different ``ck:`` ref per keyword and must merge by
        content.
        """
        if self.id not in UNSTABLE_KEY_IDS:
            return self.component_key
        slug = keyword_slug(keyword)
        if not slug:
            return self.component_key
        return f"{self.component_key}-{slug}"[:MAX_KEY_LEN].rstrip("-")

    def possible_refs(self) -> tuple[str, ...]:
        """Every ``source_ref`` the scraper may assign this post on its own keywords' pages.

        The URN when "Copy link to post" yields it (``copy_link == "urn"``), then ``ck:<KEY>``
        for every KEY it renders with (a miss or a cap of 0 leaves the ``ck:`` ref).
        """
        refs: list[str] = [self.urn] if self.copy_link == "urn" else []
        for keyword in self.keywords:
            ref = f"ck:{self.key_for(keyword)}"
            if ref not in refs:
                refs.append(ref)
        return tuple(refs)

    @property
    def text_head(self) -> str:
        return self.text if len(self.text) <= SEE_MORE_AT else self.text[:SEE_MORE_AT]

    @property
    def text_tail(self) -> str:
        return "" if len(self.text) <= SEE_MORE_AT else self.text[SEE_MORE_AT:]


def normalize_keyword(keyword: str | None) -> str:
    return " ".join((keyword or "").lower().split())


@lru_cache(maxsize=1)
def load_corpus() -> tuple[FixturePost, ...]:
    raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    return tuple(
        FixturePost(
            id=item["id"],
            urn=item["urn"],
            keywords=tuple(normalize_keyword(k) for k in item["keywords"]),
            author_name=item["author_name"],
            author_headline=item["author_headline"],
            profile_slug=item["profile_slug"],
            posted_relative=item["posted_relative"],
            text=item["text"],
            component_key=item["component_key"],
            copy_link=_copy_link(item),
            profile_kind="company" if item.get("profile_kind") == "company" else "in",
        )
        for item in raw
    )


def _copy_link(item: dict) -> CopyLink:
    mode = item["copy_link"]
    if mode not in COPY_LINK_MODES:
        raise ValueError(f"posts.json {item['id']}: copy_link must be one of {sorted(COPY_LINK_MODES)}, got {mode!r}")
    return mode


def posts_for(keyword: str | None) -> list[FixturePost]:
    """Every post tagged with ``keyword`` (case/whitespace-insensitive), in corpus order."""
    key = normalize_keyword(keyword)
    return [p for p in load_corpus() if key in p.keywords]


def post_by_urn(urn: str) -> FixturePost | None:
    return next((p for p in load_corpus() if p.urn == urn), None)


def post_by_id(post_id: str) -> FixturePost | None:
    return next((p for p in load_corpus() if p.id == post_id), None)


def permalink_miss_posts() -> list[FixturePost]:
    """The ``permalink-miss-test`` page's posts (P1, P6, P9, P2), in that order."""
    by_id = {p.id: p for p in load_corpus()}
    return [by_id[i] for i in PERMALINK_MISS_IDS]

"""The fixture post corpus (``posts.json``; architecture.md → Fixture LinkedIn corpus)."""

from __future__ import annotations

import json
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
    {CHECKPOINT_KEYWORD, CAPTCHA_KEYWORD, RATELIMIT_KEYWORD, LOGOUT_KEYWORD, DRIFT_KEYWORD, *LAZY_PLANS}
)

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
        )
        for item in raw
    )


def posts_for(keyword: str | None) -> list[FixturePost]:
    """Every post tagged with ``keyword`` (case/whitespace-insensitive), in corpus order."""
    key = normalize_keyword(keyword)
    return [p for p in load_corpus() if key in p.keywords]


def post_by_urn(urn: str) -> FixturePost | None:
    return next((p for p in load_corpus() if p.urn == urn), None)

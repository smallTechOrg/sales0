"""Scripted ``LinkedInSearcher`` stand-in for the Phase 1.4 discovery job (scan + capture_links)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sales0.scraper.types import KeywordScrapeResult, LinkCaptureResult, ScrapedPost
from tests.integration.services.helpers import LIVE


def permalink_for(ref: str) -> str:
    digest = int(hashlib.sha1(ref.encode()).hexdigest(), 16) % 10**12
    return f"{LIVE}/feed/update/urn:li:activity:75{digest:012d}/"


@dataclass
class FakeSearcher:
    """keyword -> posts (or an exception to raise from ``scan``).

    ``events`` records the call order (``("scan", kw)``, ``("capture", kw, refs)``) so tests can
    assert scan -> register -> triage -> capture_links per keyword.
    """

    results: dict[str, list[ScrapedPost] | Exception]
    enter_error: Exception | None = None
    drift: set[str] = field(default_factory=set)
    extra: dict[str, dict[str, Any]] = field(default_factory=dict)
    capture_error: Exception | None = None
    link_map: dict[str, str] | None = None  # None: every ref gets a generated permalink
    searched: list[str] = field(default_factory=list)
    events: list[tuple] = field(default_factory=list)
    captured: list[list[str]] = field(default_factory=list)
    max_posts_seen: list[int] = field(default_factory=list)
    closed: bool = False
    on_scan: Any = None  # optional callable(keyword) run at the start of scan()
    last_keyword: str = ""

    def __call__(self, _settings: Any) -> FakeSearcher:
        return self

    def __enter__(self) -> FakeSearcher:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def scan(self, keyword: str, max_posts: int) -> KeywordScrapeResult:
        self.searched.append(keyword)
        self.events.append(("scan", keyword))
        self.max_posts_seen.append(max_posts)
        self.last_keyword = keyword
        if self.on_scan is not None:
            self.on_scan(keyword)
        item = self.results.get(keyword.lower(), [])
        if isinstance(item, Exception):
            raise item
        fields: dict[str, Any] = {"parse_errors": 0, "drift_warning": keyword in self.drift}
        fields.update(self.extra.get(keyword.lower(), {}))
        return KeywordScrapeResult(keyword=keyword, posts=list(item)[:max_posts], scrolls=3, **fields)

    def capture_links(self, refs: list[str]) -> LinkResult:
        self.events.append(("capture", self.last_keyword, list(refs)))
        self.captured.append(list(refs))
        if self.capture_error is not None:
            raise self.capture_error
        if self.link_map is None:
            links = {r: permalink_for(r) for r in refs}
        else:
            links = {r: u for r, u in self.link_map.items() if r in refs}
        return LinkCaptureResult(links=links, attempts=len(refs), found=len(links))

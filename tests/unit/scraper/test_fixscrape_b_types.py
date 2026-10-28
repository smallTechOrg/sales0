"""Audit #7/#8 shared types: ``ScrapedPost.truncated``, ``KeywordScrapeResult.page_loads``, StopReason."""

from __future__ import annotations

from datetime import date
from typing import get_args

from sales0.scraper.parsing import post_from_raw
from sales0.scraper.types import KeywordScrapeResult, ScrapedPost, StopReason

RAW = {"strategy": "sdui", "urn": "urn:li:activity:7300000000000000001", "text": "Need a trainer", "truncated": True}


def test_defaults() -> None:
    k = KeywordScrapeResult(keyword="k", posts=[], scrolls=0, parse_errors=0, drift_warning=False)
    assert k.page_loads == 0
    assert "page_budget" in get_args(StopReason)
    p = ScrapedPost("urn:li:activity:1", "u", None, None, None, None, None, "t")
    assert p.truncated is False


def test_post_from_raw_carries_the_truncated_flag() -> None:
    assert post_from_raw(RAW, "https://www.linkedin.com", date(2026, 10, 27)).truncated is True
    assert post_from_raw({**RAW, "truncated": None}, "https://www.linkedin.com", date(2026, 10, 27)).truncated is False
    raw = {k: v for k, v in RAW.items() if k != "truncated"}
    assert post_from_raw(raw, "https://www.linkedin.com", date(2026, 10, 27)).truncated is False

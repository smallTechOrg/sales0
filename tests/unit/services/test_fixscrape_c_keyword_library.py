"""fixscrape #11: the v2 phrase additions (agency/RFP/empanelment/Hinglish/cities/Copilot)."""

from __future__ import annotations

import re

from sales0.services.keyword_library import (
    ALL_SUGGESTED_KEYWORDS,
    GROUP_NAMES,
    SUGGESTED_KEYWORDS,
    SUGGESTED_KEYWORDS_V2,
    suggested_key_of,
)


def test_v2_phrases_are_valid_unique_and_do_not_overlap_v1() -> None:
    assert SUGGESTED_KEYWORDS_V2 and len(SUGGESTED_KEYWORDS) == 60
    assert len(ALL_SUGGESTED_KEYWORDS) == len(SUGGESTED_KEYWORDS) + len(SUGGESTED_KEYWORDS_V2)
    assert len({k.key for k in ALL_SUGGESTED_KEYWORDS}) == len(ALL_SUGGESTED_KEYWORDS)
    assert len({k.text.lower() for k in ALL_SUGGESTED_KEYWORDS}) == len(ALL_SUGGESTED_KEYWORDS)
    for k in SUGGESTED_KEYWORDS_V2:
        assert 2 <= len(k.text) <= 100 and k.group in GROUP_NAMES
        assert k.key == suggested_key_of(k.text) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", k.key)


def test_v2_covers_the_missing_buyer_vocabulary() -> None:
    blob = " | ".join(k.text.lower() for k in SUGGESTED_KEYWORDS_V2)
    for needle in ("rfp", "empanel", "trainer requirement", "toc", "l&d", "copilot", "chahiye", "bangalore", "mumbai", "agency"):
        assert needle in blob, needle

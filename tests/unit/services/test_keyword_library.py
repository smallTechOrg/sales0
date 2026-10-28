"""The 60 suggested buyer-intent phrases (capabilities/search-setup.md). Pure data."""

from __future__ import annotations

import re
from collections import Counter

import pytest

from sales0.services.keyword_library import (
    GROUP_NAMES,
    SUGGESTED_KEYWORDS,
    SuggestedKeyword,
    suggested_key_of,
)

OFFERING_NAMES = {
    "AI corporate workshops",
    "Technical AI training",
    "AI consulting & build services",
    "Products",
}


def test_sixty_unique_keys_and_texts_fifteen_per_group() -> None:
    assert len(SUGGESTED_KEYWORDS) == 60
    assert len({k.key for k in SUGGESTED_KEYWORDS}) == 60
    assert len({k.text.lower() for k in SUGGESTED_KEYWORDS}) == 60
    counts = Counter(k.group for k in SUGGESTED_KEYWORDS)
    assert set(counts) == OFFERING_NAMES == set(GROUP_NAMES)
    assert set(counts.values()) == {15}


def test_every_phrase_is_2_to_100_chars_and_its_key_is_the_slug() -> None:
    for k in SUGGESTED_KEYWORDS:
        assert 2 <= len(k.text) <= 100, k
        assert k.text == k.text.strip() and "  " not in k.text
        assert k.key == suggested_key_of(k.text)
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", k.key) and len(k.key) <= 60


def test_the_four_legacy_fixture_keywords_are_in_the_library() -> None:
    texts = {k.text.lower() for k in SUGGESTED_KEYWORDS}
    assert {"corporate ai training", "ai consulting firm india"} <= texts
    assert "claude code trainer" in texts


def test_entries_are_immutable_and_slugs_handle_symbols_and_length() -> None:
    k = SUGGESTED_KEYWORDS[0]
    assert isinstance(k, SuggestedKeyword)
    with pytest.raises(AttributeError):
        k.key = "x"  # type: ignore[misc]
    assert suggested_key_of("  AI & ML -- Training!! ") == "ai-ml-training"
    assert suggested_key_of("a" * 100) == "a" * 60
    assert suggested_key_of("!!!") == ""

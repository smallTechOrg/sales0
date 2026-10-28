"""Phase 1.4: the fixture's bulk corpus (pure, no browser)."""

from __future__ import annotations

import re
from collections import Counter

import pytest

from sales0.linkedin_fixture import bulk
from sales0.linkedin_fixture.bulk import (
    LEGACY_KEYWORDS,
    PAGE_SIZE,
    expected_verdict,
    generate_bulk,
    is_bulk_keyword,
    keyword_hash,
    results_for,
    variant_for,
)
from sales0.linkedin_fixture.corpus import posts_for
from sales0.scraper.parsing import clean_text
from sales0.services.keyword_library import SUGGESTED_KEYWORDS

BULK_KEYWORDS = [k.text for k in SUGGESTED_KEYWORDS if is_bulk_keyword(k.text)]


def test_the_corpus_is_deterministic_and_has_300_distinct_posts() -> None:
    posts = generate_bulk()
    assert [p.id for p in posts] == [f"B{i:03d}" for i in range(1, 301)]
    assert posts == generate_bulk(300, 1404)
    assert len({p.text for p in posts}) == 300 and len({p.component_key for p in posts}) == 300
    assert len({p.urn for p in posts}) == 300 and len({p.profile_slug for p in posts}) == 300
    assert generate_bulk(300, 7) != posts


def test_kind_shares_and_verdicts() -> None:
    posts = generate_bulk()
    assert Counter(p.kind for p in posts) == {
        "buyer": 30, "agency_recruiting": 60, "trainer_ad": 45, "event_promo": 60, "job_post": 30, "thought_leadership": 75,
    }
    assert {p.expected for p in posts if p.kind == "buyer"} == {"genuine"}
    assert {p.expected for p in posts if p.kind != "buyer"} == {"discard"}


def test_everything_is_synthetic() -> None:
    for p in generate_bulk():
        assert p.author_name == f"Bulk Author {p.id[1:]}" and p.profile_slug == f"bulk-author-{p.id[1:]}"
        assert all(e.rstrip(".").endswith(".example") for e in re.findall(r"[\w.-]+@([\w.-]+)", p.text))
        assert all(ph.startswith("+91 90000 0") for ph in re.findall(r"\+91 \d{5} \d{5}", p.text))


def test_buyer_posts_hide_their_contact_in_the_see_more_tail() -> None:
    for p in (p for p in generate_bulk() if p.kind == "buyer"):
        assert "@" in p.text_tail and "+91 90000 0" in p.text_tail, p.id


def test_keyword_results_follow_the_documented_formula() -> None:
    assert BULK_KEYWORDS and len(BULK_KEYWORDS) >= 55
    bulk_posts = generate_bulk()
    for kw in BULK_KEYWORDS[:6]:
        posts = results_for(kw)
        assert posts is not None and len(posts) == 90 and len({p.id for p in posts}) == 90
        h = keyword_hash(kw)
        assert [p.id for p in posts] == [bulk_posts[(h + 7 * i) % 300].id for i in range(90)]
        assert [p.age_days for p in posts] == [i * 30 // 90 for i in range(90)]


def test_date_filters_and_sort() -> None:
    kw = BULK_KEYWORDS[0]
    week, month = results_for(kw, '"past-week"'), results_for(kw, "past-month")
    assert week is not None and month is not None
    assert len(week) == 24 and all(p.age_days <= 7 for p in week) and len(month) == 90
    day = results_for(kw, "past-24h")
    assert day is not None and all(p.age_days <= 1 for p in day)
    latest = results_for(kw, None, '"date_posted"')
    relevance = results_for(kw, None, "relevance")
    assert latest is not None and relevance is not None
    assert [p.age_days for p in latest] == sorted(p.age_days for p in latest)
    assert {p.id for p in relevance} == {p.id for p in latest} and [p.id for p in relevance] != [p.id for p in latest]
    assert results_for(kw, None, "relevance") == relevance  # stable


def test_legacy_and_unknown_keywords_are_not_bulk() -> None:
    for kw in LEGACY_KEYWORDS:
        assert results_for(kw) is None and not is_bulk_keyword(kw)
    assert results_for("something else entirely") is None
    assert posts_for("ai workshop")  # the Phase 1/1.2 P-set is untouched


def test_markup_variants_are_split_by_hash_parity_and_both_exist() -> None:
    variants = Counter(variant_for(k) for k in BULK_KEYWORDS)
    assert variants["sdui"] >= 10 and variants["listitem"] >= 10
    for kw in BULK_KEYWORDS:
        assert variant_for(kw) == ("sdui" if keyword_hash(kw) % 2 == 0 else "listitem")


def test_copy_link_never_misses_three_times_in_a_row() -> None:
    for kw in BULK_KEYWORDS[:10]:
        modes = [p.copy_link for p in results_for(kw) or []]
        assert "noop" in modes and "none" not in modes
        assert 0.1 <= modes.count("noop") / len(modes) <= 0.3
        assert "noop" * 0 == "" and not any(modes[i : i + 3] == ["noop"] * 3 for i in range(len(modes)))


def test_expected_verdict_matches_full_text_scraped_text_marker_and_prefix() -> None:
    posts = generate_bulk()
    buyer = next(p for p in posts if p.kind == "buyer")
    other = next(p for p in posts if p.kind == "event_promo")
    assert expected_verdict(buyer.text) == "genuine" and expected_verdict(other.text) == "discard"
    assert expected_verdict(clean_text(buyer.text)) == "genuine"  # what the scraper hands over
    assert expected_verdict("Prompt: ... " + buyer.text.split("\n")[-1]) == "genuine"  # the (ref Bnnn) marker
    assert expected_verdict(buyer.text_head) == "genuine"  # an unexpanded card (prefix)
    assert expected_verdict("") is None and expected_verdict("totally unrelated post text") is None


def test_constants_match_the_spec() -> None:
    assert bulk.BULK_SIZE == 300 and bulk.BULK_SEED == 1404 and PAGE_SIZE == 10 and bulk.PER_KEYWORD == 90


@pytest.mark.parametrize("age,label", [(0, "5h"), (3, "3d"), (7, "1w"), (21, "3w"), (30, "1mo")])
def test_relative_labels_parse(age: int, label: str) -> None:
    from datetime import date

    from sales0.scraper.relative_time import parse_relative

    assert bulk.relative_label(age) == label
    assert parse_relative(label, date(2026, 10, 28)) is not None

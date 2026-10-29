"""Pure dedupe normalisation: NFKC, lower-case, URLs and hashtags removed, whitespace collapsed."""

from __future__ import annotations

import hashlib

from sales0.services.dedupe import content_hash, near_duplicate, normalize_text, strip_ellipsis

P2 = (
    "SkillBridge Learning is hiring freelance Generative AI trainers for our corporate clients.\n"
    "Send your trainer profile + per-day commercials to trainers@skillbridge.example"
)
P7 = (
    "SkillBridge learning is HIRING freelance Generative AI trainers\n\nfor our corporate Clients.  "
    "Send your trainer profile + per-day commercials to trainers@skillbridge.example #hiring #trainers"
)


def test_normalize_text_lowercases_collapses_whitespace_and_strips_hashtags() -> None:
    assert normalize_text("  Hello\n\n  World\t#AI #GenAI  ") == "hello world"


def test_normalize_text_removes_urls() -> None:
    assert normalize_text("Apply at https://example.com/jobs?id=1 or www.example.org today") == "apply at or today"


def test_normalize_text_applies_nfkc() -> None:
    # Full-width letters and the "ﬁ" ligature fold to their plain forms.
    assert normalize_text("ＡＩ ﬁrst") == "ai first"


def test_content_hash_matches_for_reworded_whitespace_casing_and_hashtags() -> None:
    assert content_hash(P2) == content_hash(P7)


def test_content_hash_is_sha256_hex_of_the_normalised_text() -> None:
    assert content_hash(P2) == hashlib.sha256(normalize_text(P2).encode()).hexdigest()
    assert len(content_hash(P2)) == 64


def test_content_hash_differs_for_different_requirements() -> None:
    assert content_hash(P2) != content_hash(P2.replace("Generative AI", "SAP ABAP"))


def test_normalize_text_of_empty_or_hashtag_only_text_is_empty() -> None:
    assert normalize_text("") == ""
    assert normalize_text("   #hiring  #ai ") == ""


def test_normalize_text_removes_scheme_less_short_links() -> None:
    assert normalize_text("Details lnkd.in/gAbC12 and bit.ly/x9 plus t.co/Zz today") == "details and plus today"


def test_short_link_differences_do_not_break_content_dedupe() -> None:
    a = "Need a GenAI trainer for Pune, see lnkd.in/abc"
    b = "Need a GenAI trainer for Pune, see lnkd.in/xyz123"
    assert content_hash(a) == content_hash(b)


def test_normalize_text_keeps_emails_abbreviations_and_slashes_without_a_domain() -> None:
    text = "Mail training@acme.example, e.g. AI/ML batch at Rs.500/hr"
    assert normalize_text(text) == "mail training@acme.example, e.g. ai/ml batch at rs.500/hr"


# ---------------------------------------------------------------------- Phase 1.2: near-duplicate fingerprint
LONG = (
    "Acme Logistics is looking for a training partner to run a two-day Generative AI workshop for forty "
    "managers at our Pune office, offline, in the week of 17 November. Please send proposals with "
    "agenda, trainer profiles and commercials to training@acmelogistics.example or call +91 90000 00001. "
    "We would love to hear from firms that have done this for logistics teams before."
)
ME = "https://www.linkedin.com/in/priya-menon-fx/"


def test_strip_ellipsis_drops_trailing_more_and_ellipsis_only() -> None:
    assert strip_ellipsis("We need a partner …more") == "We need a partner"
    assert strip_ellipsis("We need a partner...more") == "We need a partner"
    assert strip_ellipsis("We need a partner… ") == "We need a partner"
    assert strip_ellipsis("We need a partner ...") == "We need a partner"
    assert strip_ellipsis("We need more") == "We need more"  # a real word "more" survives
    assert strip_ellipsis("") == ""


def test_truncated_and_expanded_text_by_the_same_author_are_near_duplicates() -> None:
    truncated = LONG[:120] + "…more"
    assert near_duplicate(ME, truncated, ME, LONG)
    assert near_duplicate(ME, LONG, ME, truncated)  # symmetric


def test_profile_slug_comparison_ignores_url_shape_and_case() -> None:
    other_shape = "https://www.linkedin.com/in/Priya-Menon-FX?miniProfileUrn=x"
    assert near_duplicate(ME, LONG[:150] + "…", other_shape, LONG)


def test_different_author_is_never_a_near_duplicate() -> None:
    assert not near_duplicate(ME, LONG[:150], "https://www.linkedin.com/in/someone-else-fx/", LONG)


def test_missing_author_on_either_side_is_never_a_near_duplicate() -> None:
    assert not near_duplicate(None, LONG, None, LONG)
    assert not near_duplicate(ME, LONG, None, LONG)
    assert not near_duplicate(ME, LONG, "https://www.linkedin.com/feed/", LONG)


def test_overlap_under_100_normalised_chars_is_not_a_near_duplicate() -> None:
    short = LONG[:90] + "…more"
    assert len(normalize_text(strip_ellipsis(short))) < 100
    assert not near_duplicate(ME, short, ME, LONG)


def test_same_author_with_a_different_opening_is_not_a_near_duplicate() -> None:
    other = "Hiring: " + LONG
    assert not near_duplicate(ME, other, ME, LONG)


def test_only_the_first_300_normalised_chars_are_compared() -> None:
    tail_differs = LONG + " Extra tail A." * 5
    assert len(normalize_text(LONG)) > 300
    assert near_duplicate(ME, LONG[:330] + " completely different ending", ME, tail_differs)

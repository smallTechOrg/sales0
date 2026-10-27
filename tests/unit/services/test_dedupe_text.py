"""Pure dedupe normalisation: NFKC, lower-case, URLs and hashtags removed, whitespace collapsed."""

from __future__ import annotations

import hashlib

from sales0.services.dedupe import content_hash, normalize_text

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

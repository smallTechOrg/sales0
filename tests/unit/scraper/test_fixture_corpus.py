"""The fixture corpus must hold the facts the run expectations depend on (architecture.md)."""

import re
import unicodedata

from sales0.linkedin_fixture.corpus import SEE_MORE_AT, load_corpus, normalize_keyword, post_by_urn, posts_for
from sales0.scraper.parsing import URN_RE


def _spec_normalize(text: str) -> str:
    # The dedupe normalisation rule from capabilities/linkedin-discovery.md.
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"#\w+", " ", text)
    return " ".join(text.split())


def _by_id() -> dict:
    return {p.id: p for p in load_corpus()}


def test_corpus_has_the_ten_specified_posts_with_valid_urns() -> None:
    posts = load_corpus()
    assert [p.id for p in posts] == ["P1", "P2", "P3", "P5", "P6", "P7", "P8", "P9", "P10", "P11"]
    for p in posts:
        assert URN_RE.match(p.urn)
        assert p.urn == f"urn:li:activity:73000000000000000{int(p.id[1:]):02d}"
    assert len({p.urn for p in posts}) == len(posts)


def test_keyword_matching_yields_the_run_expectation_counts() -> None:
    assert [p.id for p in posts_for("corporate ai training")] == ["P1", "P2", "P3"]
    assert [p.id for p in posts_for("ai workshop")] == ["P1", "P5", "P6", "P7"]
    assert [p.id for p in posts_for("ai consulting")] == ["P8", "P9"]
    assert [p.id for p in posts_for("claude code training")] == ["P10", "P11"]
    assert [p.id for p in posts_for("  AI   Workshop ")] == ["P1", "P5", "P6", "P7"]
    assert posts_for("nothing matches this") == []
    assert normalize_keyword(None) == ""


def test_p7_is_a_content_duplicate_of_p2_under_the_dedupe_rule() -> None:
    posts = _by_id()
    assert posts["P7"].text != posts["P2"].text
    assert posts["P7"].text.endswith(" #hiring #trainers")
    assert _spec_normalize(posts["P7"].text) == _spec_normalize(posts["P2"].text)
    for other in ("P1", "P3", "P5", "P6", "P8", "P9", "P10", "P11"):
        assert _spec_normalize(posts[other].text) != _spec_normalize(posts["P2"].text)


def test_posts_carry_the_facts_the_triage_expectations_rely_on() -> None:
    p = _by_id()
    assert "training@acmelogistics.example" in p["P1"].text and "+91 98200 11111" in p["P1"].text
    assert "Pune" in p["P1"].text and "offline" in p["P1"].text and "training partner" in p["P1"].text
    assert "hiring freelance Generative AI trainers" in p["P2"].text and "trainers@skillbridge.example" in p["P2"].text
    assert "individual freelance trainer" in p["P3"].text and "hourly rate" in p["P3"].text and "online" in p["P3"].text
    assert re.search(r"[ऀ-ॿ]", p["P5"].text) and "arjun.rao@zentrix.example" in p["P5"].text
    assert "Bengaluru" in p["P5"].text
    assert "Build your first AI agent" in p["P6"].text and "online" in p["P6"].text
    assert "@" not in p["P6"].text and not re.search(r"\+?\d[\d ]{7,}", p["P6"].text)
    assert "design and build" in p["P8"].text and "Mumbai" in p["P8"].text and "it-projects@meridian.example" in p["P8"].text
    assert "open to freelance" in p["P9"].text and "Connect with me!" in p["P9"].text
    assert "Claude Code training" in p["P10"].text and "lead@quantaworks.example" in p["P10"].text
    assert "onboarding freelance trainers" in p["P11"].text and "hr@briltus.example" in p["P11"].text
    assert p["P1"].author_name == "Priya Menon" and p["P1"].author_headline == "Head of L&D at Acme Logistics"
    assert p["P1"].posted_relative == "1d"


def test_long_posts_split_into_head_and_hidden_tail_without_losing_text() -> None:
    p1 = _by_id()["P1"]
    assert len(p1.text) > SEE_MORE_AT
    assert p1.text_head + p1.text_tail == p1.text
    assert "training@acmelogistics.example" in p1.text_tail  # only visible after "see more"
    short = _by_id()["P11"]
    assert short.text_tail == "" and short.text_head == short.text
    assert post_by_urn(p1.urn) is p1 and post_by_urn("urn:li:activity:0") is None

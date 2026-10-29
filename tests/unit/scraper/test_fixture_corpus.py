"""The fixture corpus must hold the facts the run expectations depend on (architecture.md).

Phase 1.2 (S4): every post also carries a synthetic SDUI ``component_key`` and a
``copy_link`` behaviour; P1's KEY varies per keyword; ``possible_refs`` lists every
``source_ref`` the scraper may assign a post.
"""

import re
import unicodedata
from urllib.parse import urlsplit

from sales0.linkedin_fixture.corpus import (
    CARD_SHAPES,
    COPY_LINK_QUERY,
    LAZY_PLANS,
    LEGACY_KEYWORD,
    LOGOUT_KEYWORD,
    PERMALINK_MISS_IDS,
    PERMALINK_MISS_KEYWORD,
    SEE_MORE_AT,
    TRIGGER_KEYWORDS,
    UNSTABLE_KEY_IDS,
    keyword_slug,
    load_corpus,
    normalize_keyword,
    permalink_miss_posts,
    post_by_urn,
    posts_for,
)
from sales0.scraper.parsing import URN_RE

LISTITEM_KEY_RE = re.compile(r"^expanded(.+?)FeedType_[A-Za-z_]+$")  # architecture.md SDUI_LISTITEM_KEY_RE
CORPUS_KEYWORDS = ("corporate ai training", "ai workshop", "ai consulting", "claude code training")


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


def test_card_shapes_rotate_by_corpus_position_as_specified() -> None:
    shapes = {p.id: p.shape for p in load_corpus()}
    assert shapes == {
        "P1": "data-urn", "P2": "data-urn", "P3": "data-urn",
        "P5": "data-id", "P6": "data-id", "P7": "data-id",
        "P8": "update-link", "P9": "update-link",
        "P10": "posts-link", "P11": "posts-link",
    }
    assert set(CARD_SHAPES) == {p.id for p in load_corpus()}  # no orphan or missing entry


def test_posts_slug_carries_the_urn_in_linkedins_posts_permalink_form() -> None:
    p10 = _by_id()["P10"]
    assert p10.activity_id == "7300000000000000010"
    match = re.fullmatch(r"[^/]*-(activity|ugcPost|share)-(\d+)-AbCd", p10.posts_slug)
    assert match and f"urn:li:{match.group(1)}:{match.group(2)}" == p10.urn
    assert p10.posts_slug.startswith(p10.profile_slug)


def test_trigger_keywords_include_phase_1_2_keywords_and_never_collide_with_corpus_keywords() -> None:
    assert LOGOUT_KEYWORD == "logout-test"
    assert LEGACY_KEYWORD == "legacy-urn-test" and PERMALINK_MISS_KEYWORD == "permalink-miss-test"
    assert TRIGGER_KEYWORDS == {
        "checkpoint-test", "captcha-test", "ratelimit-test", "logout-test", "layout-drift-test",
        "lazy-scroll-test", "checkpoint-scroll-test", "ratelimit-scroll-test",
        "legacy-urn-test", "permalink-miss-test",
    }
    corpus_keywords = {k for p in load_corpus() for k in p.keywords}
    assert not (TRIGGER_KEYWORDS & corpus_keywords)
    assert all(posts_for(k) == [] for k in TRIGGER_KEYWORDS)
    assert all(posts_for(plan.source_keyword) for plan in LAZY_PLANS.values())


# ----------------------------------------------------------------- Phase 1.2: SDUI identity
def test_every_post_has_a_unique_opaque_component_key() -> None:
    posts = load_corpus()
    keys = [p.component_key for p in posts]
    assert len(set(keys)) == len(keys)
    for key in keys:
        assert re.fullmatch(r"[A-Za-z0-9]{10,24}", key), key
        assert not LISTITEM_KEY_RE.match(key)  # an inner key never looks like a wrapper key
        assert "urn" not in key.lower() and "activity" not in key.lower()


def test_copy_link_behaviour_follows_the_spec_assignment() -> None:
    assert {p.id: p.copy_link for p in load_corpus()} == {
        "P1": "noop", "P2": "urn", "P3": "none",
        "P5": "urn", "P6": "noop", "P7": "none",
        "P8": "urn", "P9": "noop",
        "P10": "urn", "P11": "none",
    }


def test_only_p1s_key_varies_per_keyword() -> None:
    assert UNSTABLE_KEY_IDS == frozenset({"P1"})
    p = _by_id()
    a, b = p["P1"].key_for("corporate ai training"), p["P1"].key_for("ai workshop")
    assert a != b
    assert a.startswith(p["P1"].component_key + "-") and b.startswith(p["P1"].component_key + "-")
    assert p["P1"].key_for("  Corporate  AI Training ") == a  # keyword normalised first
    for post in load_corpus():
        if post.id == "P1":
            continue
        assert {post.key_for(k) for k in (*CORPUS_KEYWORDS, PERMALINK_MISS_KEYWORD)} == {post.component_key}


def test_keys_fit_source_ref_and_contain_no_whitespace() -> None:
    for post in load_corpus():
        for keyword in (*post.keywords, PERMALINK_MISS_KEYWORD, "lazy-scroll-test", "x" * 300):
            key = post.key_for(keyword)
            assert key and not re.search(r"\s", key)
            assert len(key) <= 197  # "ck:" + KEY fits enquiry_sources.source_ref String(200)
            assert not LISTITEM_KEY_RE.match(key)


def test_keyword_slug_edge_cases() -> None:
    assert keyword_slug("Corporate AI  training") == "corporate-ai-training"
    assert keyword_slug("  ") == ""
    assert keyword_slug("c++ / ai!") == "c-ai"
    assert _by_id()["P1"].key_for("") == _by_id()["P1"].component_key  # nothing to vary by


def test_possible_refs_cover_every_ref_the_scraper_can_assign() -> None:
    p = _by_id()
    # P1: noop menu, two keywords -> two distinct ck: refs, never its URN.
    assert p["P1"].possible_refs() == (
        f"ck:{p['P1'].key_for('corporate ai training')}",
        f"ck:{p['P1'].key_for('ai workshop')}",
    )
    # P2: copy link yields the URN; the ck: ref is still possible (cap 0, miss).
    assert p["P2"].possible_refs() == (p["P2"].urn, f"ck:{p['P2'].component_key}")
    # P3: no menu at all.
    assert p["P3"].possible_refs() == (f"ck:{p['P3'].component_key}",)
    for post in load_corpus():
        refs = post.possible_refs()
        assert len(refs) == len(set(refs))
        assert (post.urn in refs) == (post.copy_link == "urn")


def test_share_url_is_a_same_site_posts_link_with_tracking_and_the_urn_in_its_slug() -> None:
    base = "http://127.0.0.1:9/__fixtures/linkedin"
    p5 = _by_id()["P5"]
    url = p5.share_url(base)
    parts = urlsplit(url)
    assert url.startswith(f"{base}/posts/{p5.posts_slug}?")
    assert parts.query == COPY_LINK_QUERY
    assert "utm_source=share" in parts.query and "rcm=" in parts.query
    m = re.search(r"-(activity)-(\d+)-", parts.path)
    assert m and f"urn:li:{m.group(1)}:{m.group(2)}" == p5.urn


def test_company_authors_link_to_a_company_page() -> None:
    p = _by_id()
    assert p["P11"].profile_path == "company/briltus-academy"
    assert p["P1"].profile_path == "in/priya-menon-acme"
    assert {post.id for post in load_corpus() if post.profile_path.startswith("company/")} == {"P11"}


def test_permalink_miss_posts_are_p1_p6_p9_p2() -> None:
    assert PERMALINK_MISS_IDS == ("P1", "P6", "P9", "P2")
    assert [p.id for p in permalink_miss_posts()] == ["P1", "P6", "P9", "P2"]

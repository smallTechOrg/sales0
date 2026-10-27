"""upsert_post on real SQLite: new, URN duplicate, content duplicate, keyword hits and sources."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, EnquiryKeywordHit, EnquirySource
from sales0.services.dedupe import upsert_post
from tests.integration.services.helpers import SEARCH_URL, make_run, post, sdui_post

P2 = "SkillBridge Learning is hiring freelance Generative AI trainers. Send profile to trainers@skillbridge.example"
P7 = "SkillBridge learning is HIRING freelance\nGenerative AI trainers.  Send profile to trainers@skillbridge.example #hiring"


def test_upsert_post_inserts_a_new_enquiry_with_source_and_keyword_hit(db: Session) -> None:
    run_id = make_run()
    kind, eid = upsert_post(db, post(1, "Acme wants a GenAI workshop in Pune", author="Priya"), run_id, None, "ai workshop")
    assert kind == "new"
    e = db.get(Enquiry, eid)
    assert e is not None
    assert (e.review_status, e.triage_status, e.author_name) == ("pending_review", "pending", "Priya")
    assert e.source_url == "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/"
    assert [s.source_ref for s in e.sources] == ["urn:li:activity:7300000000000000001"]
    assert [h.keyword_text for h in e.keyword_hits] == ["ai workshop"]


def test_upsert_post_merges_same_urn_under_a_second_keyword(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, post(1, "Acme wants a GenAI workshop"), run_id, None, "corporate ai training")
    kind, again = upsert_post(db, post(1, "Acme wants a GenAI workshop"), run_id, None, "ai workshop")
    assert (kind, again) == ("dupe_urn", eid)
    hits = db.scalars(select(EnquiryKeywordHit.keyword_text).where(EnquiryKeywordHit.enquiry_id == eid)).all()
    assert sorted(hits) == ["ai workshop", "corporate ai training"]
    assert db.scalar(select(func.count(Enquiry.id))) == 1


def test_upsert_post_merges_same_text_with_different_urn(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, post(2, P2), run_id, None, "corporate ai training")
    kind, again = upsert_post(db, post(7, P7), run_id, None, "ai workshop")
    assert (kind, again) == ("dupe_content", eid)
    refs = db.scalars(select(EnquirySource.source_ref).where(EnquirySource.enquiry_id == eid)).all()
    assert len(refs) == 2
    assert db.scalar(select(func.count(Enquiry.id))) == 1
    assert db.get(Enquiry, eid).raw_text == P2  # type: ignore[union-attr]


def test_upsert_post_same_keyword_twice_in_one_run_adds_one_hit(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, post(1, "Acme wants a workshop"), run_id, None, "ai workshop")
    upsert_post(db, post(1, "Acme wants a workshop"), run_id, None, "ai workshop")
    assert db.scalar(select(func.count(EnquiryKeywordHit.id)).where(EnquiryKeywordHit.enquiry_id == eid)) == 1


def test_upsert_post_does_not_merge_two_posts_whose_text_is_only_hashtags(db: Session) -> None:
    run_id = make_run()
    a = upsert_post(db, post(20, "#hiring"), run_id, None, "ai workshop")
    b = upsert_post(db, post(21, "#ai #genai"), run_id, None, "ai workshop")
    assert a[0] == b[0] == "new" and a[1] != b[1]


def test_dupe_in_a_later_run_records_a_hit_for_that_run(db: Session) -> None:
    run1, run2 = make_run(), make_run()
    _, eid = upsert_post(db, post(1, "Acme wants a workshop"), run1, None, "ai workshop")
    kind, _ = upsert_post(db, post(1, "Acme wants a workshop"), run2, None, "ai workshop")
    assert kind == "dupe_urn"
    runs = db.scalars(select(EnquiryKeywordHit.run_id).where(EnquiryKeywordHit.enquiry_id == eid)).all()
    assert sorted(runs) == sorted([run1, run2])


# ---------------------------------------------------------------------- Phase 1.2: SDUI identity
LONG = (
    "Acme Logistics is looking for a training partner to run a two-day Generative AI workshop for forty "
    "managers at our Pune office, offline, in the week of 17 November. Please send proposals with "
    "agenda, trainer profiles and commercials to training@acmelogistics.example or call +91 90000 00001. "
    "We would love to hear from firms that have done this for logistics teams before."
)
P2_URN = "urn:li:activity:7300000000000000002"


def _refs(db: Session, eid: int) -> list[str]:
    return list(db.scalars(select(EnquirySource.source_ref).where(EnquirySource.enquiry_id == eid)
                           .order_by(EnquirySource.id)))


def test_new_sdui_post_stores_ck_ref_profile_link_and_search_url_as_source_url(db: Session) -> None:
    kind, eid = upsert_post(db, sdui_post("sxQ3v9LmP1aZ0k", LONG), make_run(), None, "ai workshop")
    assert kind == "new"
    e = db.get(Enquiry, eid)
    assert e is not None
    assert e.source_url == "https://www.linkedin.com/in/priya-menon-fx/"
    assert e.author_profile_url == "https://www.linkedin.com/in/priya-menon-fx/"
    src = db.scalars(select(EnquirySource).where(EnquirySource.enquiry_id == eid)).one()
    assert (src.source_ref, src.source_url) == ("ck:sxQ3v9LmP1aZ0k", SEARCH_URL)


def test_same_ck_ref_again_is_a_ref_duplicate(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, sdui_post("KEYA", LONG), run_id, None, "corporate ai training")
    assert upsert_post(db, sdui_post("KEYA", LONG), run_id, None, "ai workshop") == ("dupe_urn", eid)
    assert _refs(db, eid) == ["ck:KEYA"]


def test_changed_key_with_same_text_merges_by_content_and_adds_the_new_ck_row(db: Session) -> None:
    # P1 in the fixture: its key differs per keyword, the text does not.
    run_id = make_run()
    _, eid = upsert_post(db, sdui_post("KEYA-corporate-ai-training", LONG), run_id, None, "corporate ai training")
    kind, again = upsert_post(db, sdui_post("KEYA-ai-workshop", LONG), run_id, None, "ai workshop")
    assert (kind, again) == ("dupe_content", eid)
    assert _refs(db, eid) == ["ck:KEYA-corporate-ai-training", "ck:KEYA-ai-workshop"]
    hits = db.scalars(select(EnquiryKeywordHit.keyword_text).where(EnquiryKeywordHit.enquiry_id == eid)).all()
    assert sorted(hits) == ["ai workshop", "corporate ai training"]


def test_truncated_then_expanded_text_by_the_same_author_merges_as_content_duplicate(db: Session) -> None:
    run1, run2 = make_run(), make_run()
    _, eid = upsert_post(db, sdui_post("KEY1", LONG[:180] + "…more"), run1, None, "ai workshop")
    kind, again = upsert_post(db, sdui_post("KEY2", LONG), run2, None, "ai workshop")
    assert (kind, again) == ("dupe_content", eid)
    assert db.scalar(select(func.count(Enquiry.id))) == 1
    e = db.get(Enquiry, eid)
    assert e is not None and e.raw_text == LONG[:180] + "…more"  # never rewritten
    assert _refs(db, eid) == ["ck:KEY1", "ck:KEY2"]


def test_same_opening_text_by_a_different_author_is_a_new_enquiry(db: Session) -> None:
    run_id = make_run()
    a = upsert_post(db, sdui_post("KEY1", LONG[:180] + "…more"), run_id, None, "ai workshop")
    b = upsert_post(db, sdui_post("KEY2", LONG, slug="in/someone-else-fx", author="Someone Else"),
                    run_id, None, "ai workshop")
    assert a[0] == b[0] == "new" and a[1] != b[1]


def test_fingerprint_candidate_query_escapes_like_wildcards_in_the_slug(db: Session) -> None:
    run_id = make_run()
    upsert_post(db, sdui_post("KEY1", LONG[:180], slug="in/a_b%c"), run_id, None, "ai workshop")
    # "_" / "%" must not act as wildcards: "in/axbyc" is a different author.
    kind, _ = upsert_post(db, sdui_post("KEY2", LONG, slug="in/axbyc"), run_id, None, "ai workshop")
    assert kind == "new"


def test_later_permalink_upgrades_a_profile_link_and_adds_the_urn_source_row(db: Session) -> None:
    run1, run2, run3 = make_run(), make_run(), make_run()
    _, eid = upsert_post(db, sdui_post("KEYP2", LONG), run1, None, "corporate ai training")
    assert db.get(Enquiry, eid).source_url.endswith("/in/priya-menon-fx/")  # type: ignore[union-attr]
    # Run 2: "Copy link to post" yielded the URN -> ref is the URN, not seen before -> content match.
    kind, again = upsert_post(db, sdui_post("KEYP2", LONG, urn=P2_URN), run2, None, "corporate ai training")
    assert (kind, again) == ("dupe_content", eid)
    e = db.get(Enquiry, eid)
    assert e is not None and e.source_url == f"https://www.linkedin.com/feed/update/{P2_URN}/"
    assert _refs(db, eid) == ["ck:KEYP2", P2_URN]
    urn_row = db.scalars(select(EnquirySource).where(EnquirySource.source_ref == P2_URN)).one()
    assert urn_row.source_url == f"https://www.linkedin.com/feed/update/{P2_URN}/"
    # Run 3 finds the URN again under yet another key: matched at step 1 by the URN alias.
    kind3, again3 = upsert_post(db, sdui_post("KEYZ", "unrelated text " * 20, urn=P2_URN), run3, None, "x")
    assert (kind3, again3) == ("dupe_urn", eid)


def test_a_permalink_is_never_downgraded_by_a_later_profile_sighting(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, sdui_post("KEYP5", LONG, urn=P2_URN), run_id, None, "ai workshop")
    upsert_post(db, sdui_post("KEYP5b", LONG), run_id, None, "ai workshop")
    e = db.get(Enquiry, eid)
    assert e is not None and e.source_url == f"https://www.linkedin.com/feed/update/{P2_URN}/"


def test_copied_posts_link_without_urn_upgrades_and_is_stored_on_the_source(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, sdui_post("KEYQ", LONG), run_id, None, "ai workshop")
    link = "https://www.linkedin.com/posts/priya-menon-fx_genai-workshop-AbCd/"
    kind, _ = upsert_post(db, sdui_post("KEYQ", LONG, link=link), run_id, None, "ai workshop")
    assert kind == "dupe_urn"
    assert db.get(Enquiry, eid).source_url == link  # type: ignore[union-attr]


def test_empty_text_sdui_posts_never_merge_with_each_other(db: Session) -> None:
    run_id = make_run()
    a = upsert_post(db, sdui_post("E1", "#hiring"), run_id, None, "ai workshop")
    b = upsert_post(db, sdui_post("E2", "#ai"), run_id, None, "ai workshop")
    assert a[0] == b[0] == "new" and a[1] != b[1]


def test_post_without_author_or_permalink_links_to_the_search_url(db: Session) -> None:
    _, eid = upsert_post(db, sdui_post("KEYS", LONG, slug=""), make_run(), None, "ai workshop")
    assert db.get(Enquiry, eid).source_url == SEARCH_URL  # type: ignore[union-attr]

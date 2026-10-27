"""upsert_post on real SQLite: new, URN duplicate, content duplicate, keyword hits and sources."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, EnquiryKeywordHit, EnquirySource
from sales0.services.dedupe import upsert_post
from tests.integration.services.helpers import make_run, post

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

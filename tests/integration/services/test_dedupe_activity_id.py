"""Audit #19: ``enquiries.activity_id`` is stored, indexed, and used by the dedupe instead of LIKE scans."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry
from sales0.services.dedupe import activity_id_of, upsert_post
from tests.integration.services.helpers import LIVE, make_run, post, sdui_post

URN = "urn:li:activity:7300000000000000001"


def test_activity_id_of_reads_urns_and_post_slugs() -> None:
    assert activity_id_of("urn:li:activity:7300000000000000001") == "7300000000000000001"
    assert activity_id_of(None, "https://x/posts/p_hello-activity-7300000000000000003-AbCd") == "7300000000000000003"
    assert activity_id_of("urn:li:ugcPost:7300000000000000009") == "7300000000000000009"
    assert activity_id_of("https://www.linkedin.com/in/x/recent-activity/all/", "ck:ABC", None) is None


def test_new_enquiry_stores_the_activity_id(db: Session) -> None:
    _, eid = upsert_post(db, post(1, "Acme wants a GenAI workshop in Pune"), make_run(), None, "k")
    assert db.get(Enquiry, eid).activity_id == "7300000000000000001"  # type: ignore[union-attr]
    assert db.scalar(select(Enquiry.id).where(Enquiry.activity_id == "7300000000000000001")) == eid


def test_same_post_seen_under_a_ck_ref_with_other_text_matches_by_activity_id(db: Session) -> None:
    run_id = make_run()
    _, eid = upsert_post(db, post(1, "Acme wants a GenAI workshop in Pune for 40 managers next month"), run_id, None, "a")
    again = sdui_post("CKX", "Totally different expanded text of the very same post " * 4, slug="in/someone-else",
                      link=f"{LIVE}/feed/update/{URN}/")
    kind, matched = upsert_post(db, again, run_id, None, "b")
    assert (kind, matched) == ("dupe_content", eid)


def test_a_late_permalink_fills_a_missing_activity_id(db: Session) -> None:
    run_id = make_run()
    first = sdui_post("CK1", "Acme wants a GenAI workshop in Pune for 40 managers next month and more words here" * 2)
    _, eid = upsert_post(db, first, run_id, None, "a")
    assert db.get(Enquiry, eid).activity_id is None  # type: ignore[union-attr]
    later = sdui_post("CK1", first.text, urn=URN)
    upsert_post(db, later, run_id, None, "b")
    assert db.get(Enquiry, eid).activity_id == "7300000000000000001"  # type: ignore[union-attr]

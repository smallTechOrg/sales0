"""E3 through the real app: category chip / select / filter on the Inbox card, the competitor offer, the accept guard,
admin-only filing, and the category control on the prospect page."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Competitor, Enquiry, Prospect
from tests.integration.members.conftest import activate, add_member, admin, env, make_client  # noqa: F401
from tests.integration.web.conftest import HX, make_enquiry, triaged


def _enq(post: str, **extra) -> int:  # noqa: ANN003
    fields = triaged("genuine", "Buyer", summary=f"{post} workshop")
    fields.update(extra)
    return make_enquiry(post, **fields)


def test_enquiry_cards_no_longer_show_or_filter_a_category(client: TestClient) -> None:
    """q23: the category lives on the company (characteristic Partner / Direct client), not on the enquiry."""
    _enq("P1", company="Plain Co")
    partner = _enq("P2", company="Partner Co", category="channel_partner", ai_category="channel_partner")
    page = client.get("/inbox").text
    assert f'id="enquiry-{partner}-category"' not in page and 'name="set_category"' not in page
    assert 'data-category="channel_partner"' not in page and 'id="inbox-category"' not in page


def test_accepting_a_partner_ticks_partner_and_a_competitor_is_offered_the_registry(client: TestClient) -> None:
    partner = _enq("P3", company="Partner Co", category="channel_partner")
    assert 'data-ai-suggested="1"' in client.get("/inbox").text  # Partner pre-ticked on the accept form
    assert client.post("/inbox/decide", data={"enquiry_ids": partner, "decision": "accept"}, headers=HX).status_code == 200
    with session_scope() as db:
        from sales0.services.crm import characteristics

        pid = db.get(Enquiry, partner).prospect_id
        assert [c.name for c in characteristics.for_prospects(db, [pid])[pid]] == ["Partner"]
    rival = _enq("P8", company="Rival Academy", category="competitor", ai_category="competitor")
    card = client.get("/inbox").text
    assert f'id="file-competitor-{rival}"' in card and "File in Competitors" in card and "Accept anyway (add the company)" in card
    # the plain Accept is refused for a Competitor ...
    blocked = client.post("/inbox/decide", data={"enquiry_ids": rival, "decision": "accept"}, headers=HX)
    assert "categorised as Competitor" in blocked.text
    with session_scope() as db:
        assert db.get(Enquiry, rival).review_status == "pending_review"
    # ... filing it moves the company to the registry and keeps the post as evidence
    filed = client.post(f"/inbox/{rival}/competitor", data={"name": "Rival Academy", "tab": "review"}, headers=HX)
    assert filed.status_code == 200 and "Filed in Competitors: Rival Academy" in filed.text
    with session_scope() as db:
        e = db.get(Enquiry, rival)
        assert e.review_status == "discarded" and e.prospect_id is None
        assert db.scalar(select(Competitor.id).where(Competitor.name == "Rival Academy")) is not None
    assert "Filed in Competitors: Rival Academy" in client.get("/inbox?tab=discarded").text


def test_accept_anyway_resets_the_category(client: TestClient) -> None:
    rival = _enq("P5", company="Odd Academy", category="competitor")
    ok = client.post("/inbox/decide", data={"enquiry_ids": rival, "decision": "accept", "as_prospect": "1"}, headers=HX)
    assert ok.status_code == 200
    with session_scope() as db:
        e = db.get(Enquiry, rival)
        assert e.review_status == "accepted" and e.category == "relevant_prospect" and e.prospect_id is not None


def test_company_page_and_pipeline_have_no_enquiry_category_controls(client: TestClient) -> None:
    eid = _enq("P10", company="Pagey Co", review_status="accepted")
    with session_scope() as db:
        from sales0.services.crm import prospects

        pid = prospects.ensure_for_enquiry(db, eid).id
    page = client.get(f"/pipeline/prospects/{pid}").text
    assert f'id="enquiry-category-{eid}"' not in page
    assert 'id="f-category"' not in client.get("/pipeline").text
    with session_scope() as db:
        assert db.get(Prospect, pid) is not None


def test_members_cannot_file_competitors_but_can_accept_anyway(
    admin: TestClient, make_client: Callable[[], TestClient]  # noqa: F811
) -> None:
    link = add_member(admin, "Mia Member", "mia@acct.example")
    mia = activate(make_client, link, "mia@acct.example")
    rival = _enq("P9", company="Member Rival", category="competitor")
    page = mia.get("/inbox").text
    assert f'id="file-competitor-{rival}"' not in page and "an admin files it" in page
    assert mia.post(f"/inbox/{rival}/competitor", data={"name": "Member Rival"}).status_code == 403
    assert admin.post(f"/inbox/{rival}/competitor", data={"name": "Member Rival"}, headers=HX).status_code == 200

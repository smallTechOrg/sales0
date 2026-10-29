"""Cross-slice checks after merging the CRM, outreach and inbox feedback slices (synthetic data, real SQLite).

- Accept on an enquiry marked "Already in pipeline" joins the existing prospect AND applies the ticked
  characteristics; "Already a client" on a marked enquiry makes that prospect the client.
- The card carries both the Accept extras (characteristics / Already a client) and the free-text discard reason.
- The prospect page renders the contacts editor (CRM), follow-ups + Log contact (outreach) and link labels (inbox).
- Compose's recipient dropdown follows e-mail edits made through the contacts editor.
"""

from __future__ import annotations

from collections.abc import Iterator
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Characteristic, Client, Enquiry, Prospect
from sales0.services.crm import characteristics, pipeline_match, prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry, triaged


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _trait(key: str) -> int:
    with session_scope() as db:
        tid = db.scalar(select(Characteristic.id).where(Characteristic.system_key == key))
        assert tid is not None
        return tid


def _enquiry(post: str, **extra: object) -> int:
    fields = triaged("genuine", "Company seeking a training partner", summary="Acme Logistics - GenAI workshop")
    fields.update(company="Acme Logistics", email="priya@acme.example", contact_person="Priya Menon")
    fields.update(extra)
    return make_enquiry(post, **fields)


def _existing_prospect() -> int:
    eid = _enquiry("P1", review_status="accepted")
    with session_scope() as db:
        return prospects.ensure_for_enquiry(db, eid).id


def _marked(post: str) -> int:
    """A new pending enquiry from the same company, marked "Already in pipeline" as a scrape run would."""
    eid = _enquiry(post, email=None)
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None
        pipeline_match.apply(db, e, run_id=None)
        assert e.pipeline_prospect_id is not None and e.review_status == "pending_review"
    return eid


def _card(html: str, eid: int) -> str:
    return html.split(f'id="enquiry-{eid}"', 1)[1].split("</article>", 1)[0]


def test_marked_card_has_accept_extras_and_free_text_discard(web: TestClient) -> None:
    pid = _existing_prospect()
    eid = _marked("P2")
    card = _card(web.get("/inbox").text, eid)
    assert "Already in pipeline" in card and f'href="/pipeline/prospects/{pid}"' in card
    assert 'name="characteristic_ids"' in card and 'name="as_client"' in card  # CRM Accept extras
    assert 'name="custom_reason"' in card  # inbox free-text discard reason
    r = web.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "discard",
                                         "custom_reason": "Agency reselling to its client"}, headers=HX)
    assert r.status_code == 200
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None and (e.review_status, e.discard_reason) == ("discarded", "Agency reselling to its client")


def test_accept_on_a_marked_enquiry_joins_the_prospect_and_applies_characteristics(web: TestClient) -> None:
    pid = _existing_prospect()
    eid = _marked("P2")
    partner = _trait("partner")
    with session_scope() as db:
        before = db.scalar(select(func.count(Prospect.id)))
    r = web.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "accept",
                                         "characteristic_ids": [str(partner)]}, headers=HX)
    assert r.status_code == 200
    with session_scope() as db:
        assert db.scalar(select(Enquiry.prospect_id).where(Enquiry.id == eid)) == pid
        assert db.scalar(select(func.count(Prospect.id))) == before
        assert [c.name for c in characteristics.of(db, "prospect", pid)] == ["Partner"]


def test_already_a_client_on_a_marked_enquiry(web: TestClient) -> None:
    pid = _existing_prospect()
    eid = _marked("P2")
    r = web.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "accept", "as_client": "1"}, headers=HX)
    assert r.status_code == 200
    with session_scope() as db:
        assert db.scalar(select(Enquiry.prospect_id).where(Enquiry.id == eid)) == pid
        assert db.scalar(select(func.count(Client.id)).where(Client.prospect_id == pid)) == 1


def test_prospect_page_renders_every_slice_and_compose_follows_email_edits(web: TestClient) -> None:
    pid = _existing_prospect()
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert 'id="company-people"' in page and "contact-edit-" in page  # CRM contacts editor
    assert 'id="follow-ups"' in page and 'id="log-contact-form"' in page and 'name="channel"' in page  # outreach
    linked = page.split('id="company-enquiries"', 1)[1].split("</section>", 1)[0]
    assert "View post" in linked  # inbox link label (post permalink)
    assert 'id="characteristics"' in page
    with session_scope() as db:
        cid = db.get(Prospect, pid).contact_id  # type: ignore[union-attr]
    back = f"/pipeline/prospects/{pid}"
    web.post(f"/contacts/{cid}/emails", data={"email": "priya.new@acme.example", "primary": "1", "next": back})
    html = unescape(web.get(f"/outreach/compose?prospect_id={pid}").text)
    pick = html.split('id="recipient-pick"', 1)[1].split("</select>", 1)[0]
    assert 'value="priya.new@acme.example"' in pick and 'value="priya@acme.example"' in pick
    assert pick.index("priya.new@acme.example") < pick.index('value="priya@acme.example"')  # new primary first

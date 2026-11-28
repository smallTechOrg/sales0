"""SMA-59: an enquiry has a full page of its own (every detail, its LinkedIn links, status and edit), and the lists link
to it and show the LinkedIn post and author links."""

from __future__ import annotations

from html import unescape

from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from sales0.services.crm import prospects
from tests.integration.web.conftest import make_enquiry, triaged

POST = "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/"
AUTHOR = "https://www.linkedin.com/in/asha-rao/"


def _accepted() -> tuple[int, int]:
    fields = {**triaged("genuine", "Company seeking a GenAI workshop", summary="GenAI workshop for 40 managers"),
              "review_status": "accepted", "company": "Page Co", "contact_person": "Asha Rao", "email": "asha@page.example",
              "phone": "+91 98200 11111", "city": "Pune", "notes": "Prefers mornings"}
    eid = make_enquiry("P1", **fields)
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        e.author_profile_url = AUTHOR
        db.flush()
        prospects.ensure_for_enquiry(db, eid)
        return eid, db.get(Enquiry, eid).company_id


def test_the_enquiry_page_shows_every_detail_and_its_links(client: TestClient) -> None:
    eid, company_id = _accepted()
    page = unescape(client.get(f"/enquiries/{eid}").text)
    assert 'id="enquiry-page"' in page and "GenAI workshop for 40 managers" in page
    assert f'id="enquiry-post-link" href="{POST}"' in page and f'id="enquiry-author-link" href="{AUTHOR}"' in page
    for text in ("Asha Rao", "mailto:asha@page.example", "+91 98200 11111", "Pune", "Prefers mornings",
                 "Company seeking a GenAI workshop", f'href="/companies/{company_id}"', "Full post text"):
        assert text in page, text
    assert 'id="enquiry-status-form"' in page and f'id="enquiry-edit-{eid}"' in page


def test_status_and_edit_from_the_page_come_back_to_it(client: TestClient) -> None:
    eid, _ = _accepted()
    resp = client.post(f"/pipeline/enquiries/{eid}/status", data={"status": "contacted", "next": f"/enquiries/{eid}"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith(f"/enquiries/{eid}")
    with session_scope() as db:
        assert db.get(Enquiry, eid).deal_status == "contacted"


def test_the_lists_link_to_the_page_and_show_the_linkedin_links(client: TestClient) -> None:
    eid, company_id = _accepted()
    board = unescape(client.get("/pipeline?tab=enquiries").text)
    assert f'href="/enquiries/{eid}"' in board and f'href="{AUTHOR}"' in board and ">LinkedIn post<" in board
    assert f'href="/enquiries/{eid}"' in client.get(f"/companies/{company_id}").text
    inbox = unescape(client.get("/inbox?tab=accepted").text)
    assert f'href="/enquiries/{eid}"' in inbox and f'class="enquiry__author-link" href="{AUTHOR}"' in inbox


def test_an_unknown_enquiry_is_not_found(client: TestClient) -> None:
    assert client.get("/enquiries/424242").status_code == 404

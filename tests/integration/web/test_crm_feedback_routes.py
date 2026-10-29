"""CRM feedback slice through the real app: Accept with characteristics / straight to client, characteristic pickers
and filters, the registry, the Companies list + roll-up page, inline contact editing from the prospect page, and
"Add person to track" on /tracked (no LinkedIn request is ever made)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Characteristic, Client, Enquiry, LinkedinProfile, Prospect
from sales0.services.crm import characteristics, contacts, prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry, triaged

JSON = {"Accept": "application/json"}


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _trait(key: str) -> int:
    with session_scope() as db:
        tid = db.scalar(select(Characteristic.id).where(Characteristic.system_key == key))
        assert tid is not None
        return tid


def _pending(post: str = "P1", poster_type: str = "buyer_company", **extra: object) -> int:
    fields = triaged("genuine", "Company seeking a training partner", summary="Acme Logistics - GenAI workshop", poster_type=poster_type)
    fields.update(company="Acme Logistics", email="priya@acme.example", contact_person="Priya Menon")
    fields.update(extra)
    return make_enquiry(post, **fields)


def _prospect(post: str = "P1", **extra: object) -> int:
    eid = _pending(post, review_status="accepted", **extra)
    with session_scope() as db:
        return prospects.ensure_for_enquiry(db, eid).id


# ---------------------------------------------------------------------------- B1
def test_accept_form_offers_characteristics_with_ai_pre_tick(web: TestClient) -> None:
    agency = _pending("P1", poster_type="training_agency")
    buyer = _pending("P2", poster_type="buyer_company")
    html = web.get("/inbox").text
    agency_card = html.split(f'id="enquiry-{agency}"', 1)[1].split("</article>", 1)[0]
    buyer_card = html.split(f'id="enquiry-{buyer}"', 1)[1].split("</article>", 1)[0]
    tp = _trait("training_provider")
    assert f'value="{tp}" checked data-ai-suggested="1"' in agency_card and "(AI suggestion)" in agency_card
    assert 'data-ai-suggested' not in buyer_card and 'name="as_client"' in buyer_card and "Already a client" in buyer_card


def test_accept_with_characteristics_and_as_client(web: TestClient) -> None:
    eid = _pending()
    partner = _trait("partner")
    resp = web.post("/inbox/decide", data={"enquiry_ids": str(eid), "decision": "accept", "characteristic_ids": [str(partner)],
                                           "as_client": "1"}, headers=HX)
    assert resp.status_code == 200
    with session_scope() as db:
        e = db.get(Enquiry, eid)
        assert e is not None and e.review_status == "accepted" and e.prospect_id
        client = db.scalar(select(Client).where(Client.prospect_id == e.prospect_id))
        assert client is not None
        cid, pid = client.id, e.prospect_id
    page = web.get(f"/pipeline/clients/{cid}").text
    assert 'data-traits="Partner"' in page and 'id="characteristics"' in page
    assert 'data-traits="Partner"' in web.get(f"/pipeline/prospects/{pid}").text
    # a plain accept without extras still works and adds nothing
    other = _pending("P2", company="Globex Ltd", email="g@globex.example")
    web.post("/inbox/decide", data={"enquiry_ids": str(other), "decision": "accept"}, headers=HX)
    with session_scope() as db:
        p2 = db.scalar(select(Enquiry.prospect_id).where(Enquiry.id == other))
        assert p2 and characteristics.of(db, "prospect", p2) == []


def test_edit_characteristics_on_pages_and_filter_lists(web: TestClient) -> None:
    pid = _prospect()
    tp, partner = _trait("training_provider"), _trait("partner")
    resp = web.post(f"/pipeline/prospects/{pid}/characteristics", data={"characteristic_ids": [str(tp), str(partner)]}, headers=HX)
    assert resp.status_code == 200 and 'hx-swap-oob="outerHTML:#company-header"' in resp.text and "Characteristics saved" in resp.text
    other = _prospect("P2", company="Globex", email="g@globex.example", contact_person="Gina")
    with session_scope() as db:
        cid, ocid = db.get(Prospect, pid).company_id, db.get(Prospect, other).company_id  # type: ignore[union-attr]
    listed = web.get(f"/pipeline?layout=list&characteristic={partner}").text
    assert f'id="company-{cid}"' in listed and f'id="company-{ocid}"' not in listed
    board = web.get(f"/pipeline?characteristic={tp}").text
    assert f'id="company-{cid}"' in board and f'id="company-{ocid}"' not in board
    assert 'id="f-characteristic"' in board
    # plain form post (no htmx): 303 back + flash; clearing all
    plain = web.post(f"/pipeline/prospects/{pid}/characteristics", data={"present": "1"}, follow_redirects=False)
    assert plain.status_code == 303
    with session_scope() as db:
        assert characteristics.of(db, "prospect", pid) == []
    bad = web.post(f"/pipeline/prospects/{pid}/characteristics", data={"characteristic_ids": ["abc"]}, headers=HX)
    assert "toast--error" in bad.text


def test_registry_add_rename_turn_off(web: TestClient) -> None:
    page = web.get("/competitors").text
    assert 'id="characteristics-registry"' in page and "Training provider" in page and "Outreach client" in page
    created = web.post("/competitors/characteristic", data={"name": "Reseller"}, headers=JSON).json()
    assert created["ok"] and created["data"]["name"] == "Reseller"
    tid = created["data"]["id"]
    dup = web.post("/competitors/characteristic", data={"name": "reseller"}, headers=JSON)
    assert dup.status_code == 400
    web.post(f"/competitors/characteristic/{tid}", data={"name": "Value reseller"}, follow_redirects=False)
    off = web.post(f"/competitors/characteristic/{tid}", data={"active": "false"}, headers=JSON).json()
    assert off["data"]["name"] == "Value reseller" and off["data"]["active"] is False
    pid = _prospect()
    assert "Value reseller" not in web.get(f"/pipeline/prospects/{pid}").text.split('id="characteristics-form"', 1)[1].split("</form>", 1)[0]


# ---------------------------------------------------------------------------- B2
def test_companies_list_search_and_rollup_page(web: TestClient) -> None:
    pid = _prospect()
    with session_scope() as db:
        company_id = db.get(Prospect, pid).company_id  # type: ignore[union-attr]
    html = web.get("/companies").text
    assert 'id="company-list"' in html and f'href="/companies/{company_id}"' in html and "Acme Logistics" in html
    assert 'href="/companies"' in html.split('aria-label="Primary"', 1)[1].split("</nav>", 1)[0]  # in the nav
    frag = web.get("/companies?q=acme", headers={**HX, "HX-Target": "company-results"})
    assert "<html" not in frag.text and "Acme Logistics" in frag.text
    assert "No companies match these filters." in web.get("/companies?q=zzz").text
    page = web.get(f"/companies/{company_id}").text
    assert 'id="company-enquiries"' in page and 'id="enquiry-' in page
    assert "View post" in page and 'id="company-outreach"' in page and "priya@acme.example" in page
    # the old prospect URL lands on the company page
    assert web.get(f"/pipeline/prospects/{pid}", follow_redirects=False).headers["location"] == f"/companies/{company_id}"
    # edit + merge
    saved = web.post(f"/companies/{company_id}", data={"website": "acme-logistics.example"}, headers=JSON).json()
    assert saved["data"]["website"] == "acme-logistics.example"
    with session_scope() as db:
        from sales0.services.crm import companies

        dup = companies.get_or_create_row(db, "Acme Logistics Pvt", website="https://acme-logistics.example/")
        assert dup is not None and dup.id == company_id  # same domain: matched, not duplicated
        other = companies.get_or_create_row(db, "Acme Logistic")
        assert other is not None
        other_id = other.id
    assert f'value="{other_id}"' in web.get(f"/companies/{company_id}").text  # offered as a duplicate
    merged = web.post(f"/companies/{company_id}/merge", data={"drop_id": str(other_id)}, headers=HX)
    assert merged.status_code == 204 and merged.headers["hx-redirect"] == f"/companies/{company_id}"
    assert web.get(f"/companies/{other_id}").status_code == 404


# ---------------------------------------------------------------------------- B3
def test_edit_contact_details_inline_from_the_prospect_page(web: TestClient) -> None:
    pid = _prospect()
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert "Edit e-mail, phone, details" in page and 'id="add-company-person"' in page
    with session_scope() as db:
        cid = db.get(Prospect, pid).contact_id  # type: ignore[union-attr]
    back = f"/pipeline/prospects/{pid}"
    r = web.post(f"/contacts/{cid}/emails", data={"email": "priya.m@acme.example", "primary": "1", "next": back}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == back
    r = web.post(f"/contacts/{cid}/phones", data={"phone": "098765 43210", "next": back}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        d = contacts.get_contact(db, cid)
        assert d.email == "priya.m@acme.example" and d.phone == "+919876543210"
        old = next(e.id for e in d.email_items if e.email == "priya@acme.example")
    web.post(f"/contacts/{cid}/emails/{old}/delete", data={"next": back})
    bad = web.post(f"/contacts/{cid}/emails", data={"email": "nope", "next": back}, follow_redirects=False)
    assert bad.status_code == 303 and bad.headers["location"] == back  # error flash on the same page
    assert "not a valid e-mail" in web.get(back).text
    # an open-redirect attempt falls back to the contact page
    evil = web.post(f"/contacts/{cid}/phones", data={"phone": "+44 20 7946 0958", "next": "https://evil.example/"}, follow_redirects=False)
    assert evil.headers["location"] == f"/contacts/{cid}"
    # partial details form from the prospect page keeps the notes
    with session_scope() as db:
        contacts.update_contact(db, cid, notes="keep me")
    web.post(f"/contacts/{cid}", data={"full_name": "Priya M", "title": "L&D Head", "company": "Acme Logistics",
                                       "linkedin_url": "https://www.linkedin.com/in/priya-m/", "next": back})
    with session_scope() as db:
        d = contacts.get_contact(db, cid)
        assert (d.full_name, d.title, d.notes, d.linkedin_url) == ("Priya M", "L&D Head", "keep me", "https://www.linkedin.com/in/priya-m/")
        assert [e.email for e in d.email_items] == ["priya.m@acme.example"]
    # add a person from the prospect page
    web.post(f"/pipeline/prospects/{pid}/contacts", data={"full_name": "Ravi", "email": "ravi@acme.example"})
    assert "ravi@acme.example" in web.get(back).text


def test_prospect_edit_details_website_and_profile(web: TestClient) -> None:
    pid = _prospect()
    r = web.post(f"/pipeline/prospects/{pid}/edit", data={"name": "Acme Logistics", "company": "Acme Logistics",
                                                         "website": "acme.example", "company_linkedin_url": "https://www.linkedin.com/company/acme-logistics/",
                                                         "linkedin_url": "https://www.linkedin.com/in/new-person/"}, headers=HX)
    assert r.status_code == 200 and "Details saved" in r.text
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert 'value="acme.example"' in page and "https://www.linkedin.com/in/new-person/" in page
    bad = web.post(f"/pipeline/prospects/{pid}/edit", data={"name": "Acme Logistics", "linkedin_url": "https://www.linkedin.com/company/x/"}, headers=HX)
    assert "toast--error" in bad.text and "linkedin.com/in/" in bad.text


# ---------------------------------------------------------------------------- B4
def test_add_person_to_track(web: TestClient) -> None:
    page = web.get("/tracked").text
    assert 'id="track-add-form"' in page and 'id="tracked-help"' in page and "Add person to track" in page
    assert 'id="tracked-empty"' in page and "Paste a person's LinkedIn profile link" in page
    r = web.post("/tracked/add", data={"profile_url": "https://www.linkedin.com/in/Asha-Rao-12/?trk=x", "name": "Asha Rao",
                                       "note": "L&D head at Acme"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tracked"
    html = web.get("/tracked").text
    assert "Now tracking Asha Rao" in html and 'data-slug="in/asha-rao-12"' in html and "L&amp;D head at Acme" in html
    with session_scope() as db:
        prof = db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug == "in/asha-rao-12"))
        assert prof is not None and prof.tracked is True and prof.posts_seen == 0
    again = web.post("/tracked/add", data={"profile_url": "linkedin.com/in/asha-rao-12"})
    assert "already tracked" in again.text
    for url, needle in (("https://www.linkedin.com/company/acme/", "company page"),
                        ("https://www.linkedin.com/search/results/people/?keywords=hr", "search page"),
                        ("https://example.com/x", "not a LinkedIn link")):
        bad = web.post("/tracked/add", data={"profile_url": url})
        assert needle in bad.text and 'flash--error' in bad.text
    with session_scope() as db:
        assert db.scalar(select(LinkedinProfile).where(LinkedinProfile.slug.like("company/%"))) is None


def test_track_from_the_contact_page(web: TestClient) -> None:
    with session_scope() as db:
        cid = contacts.upsert(db, full_name="Meera", linkedin_url="https://www.linkedin.com/in/meera-k/").id
    page = web.get(f"/contacts/{cid}").text
    assert 'id="btn-track"' in page
    r = web.post("/tracked/add", data={"profile_url": "https://www.linkedin.com/in/meera-k/", "next": f"/contacts/{cid}"}, follow_redirects=False)
    assert r.headers["location"] == f"/contacts/{cid}"
    after = web.get(f"/contacts/{cid}").text
    assert 'id="tracked-state"' in after and "Stop tracking" in after
    with session_scope() as db:
        none = contacts.upsert(db, full_name="No Link Person", email="nolink@x.example").id
    assert 'id="track-hint"' in web.get(f"/contacts/{none}").text

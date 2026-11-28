"""SMA-56 (a person's location), SMA-58 (one contact per person: the post's author is not saved again as the
company's person; search to merge people) and SMA-62 (search to merge a company saved under another name)."""

from __future__ import annotations

from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Company, Contact, TeamMember
from sales0.services.crm import companies, contacts, prospects
from tests.integration.web.conftest import HX, make_enquiry, triaged


def _admin() -> None:
    with session_scope() as db:
        db.add(TeamMember(name="Ada Admin", email="ada@example.com", role="admin"))


def _company(name: str, **kw: object) -> int:
    with session_scope() as db:
        return companies.get_or_create(db, name, **kw).id if kw else companies.get_or_create(db, name).id


def test_location_is_added_edited_and_shown(client: TestClient) -> None:
    cid = _company("Loc Co")
    resp = client.post(f"/companies/{cid}/people", data={"full_name": "Ravi Kumar", "location": "Pune, India"}, follow_redirects=False)
    assert resp.status_code in (200, 303)
    with session_scope() as db:
        person = db.scalar(select(Contact).where(Contact.full_name == "Ravi Kumar"))
        assert person is not None and person.location == "Pune, India"
        pid = person.id
    assert "Pune, India" in client.get(f"/companies/{cid}").text
    client.post(f"/contacts/{pid}", data={"location": "  Mumbai   India "}, follow_redirects=False)
    page = unescape(client.get(f"/contacts/{pid}").text)
    assert 'id="contact-location">Mumbai India<' in page and 'name="location" value="Mumbai India"' in page
    assert "Mumbai India" in client.get("/contacts").text


def test_the_post_author_named_as_the_contact_is_one_person(db_url: str) -> None:
    fields = {**triaged("genuine", "Buyer", summary="Need a workshop"), "review_status": "accepted", "company": "Same Co",
              "contact_person": "Asha Rao", "email": "asha@same.example"}
    eid = make_enquiry("P1", **fields)
    with session_scope() as db:
        from sales0.db.models import Enquiry

        e = db.get(Enquiry, eid)
        e.author_name = "Asha Rao"
        db.flush()
        from tests.integration.crm import helpers as h

        e.author_profile_id = h.profile(db, slug="in/asha-rao", name="Asha Rao").id
        db.flush()
        prospects.ensure_for_enquiry(db, eid)
    with session_scope() as db:
        rows = list(db.scalars(select(Contact).where(Contact.full_name == "Asha Rao")))
        assert len(rows) == 1  # not one "author" and one company person
        assert rows[0].linkedin_slug == "in/asha-rao" and rows[0].email == "asha@same.example"


def test_a_person_added_again_at_the_same_company_is_matched(db_url: str) -> None:
    with session_scope() as db:
        co = companies.get_or_create(db, "Match Co")
        first = contacts.upsert(db, full_name="Meera Iyer", linkedin_url="https://www.linkedin.com/in/meera-iyer/", company_id=co.id)
        again = contacts.upsert(db, full_name="Meera Iyer", email="meera@match.example", company_id=co.id)
        assert again.id == first.id
        other = contacts.upsert(db, full_name="Meera Iyer", linkedin_url="https://www.linkedin.com/in/meera-iyer-2/", company_id=co.id)
        assert other.id != first.id  # a different LinkedIn profile is someone else
        elsewhere = contacts.upsert(db, full_name="Meera Iyer", email="meera@else.example")
        assert elsewhere.id != first.id  # no company: no name-only match
        assert int(db.scalar(select(func.count()).select_from(Contact).where(Contact.full_name == "Meera Iyer")) or 0) == 3


def test_company_merge_search_finds_a_duplicate_with_another_name(client: TestClient) -> None:
    _admin()
    keep = _company("Acme Learning")
    drop = _company("ACM Training Pvt Ltd")
    page = client.get(f"/companies/{keep}").text
    assert 'id="merge-q"' in page and ">ACM Training Pvt Ltd" not in page  # not a look-alike by name
    found = client.get(f"/companies/{keep}/merge-candidates", params={"merge_q": "acm train"}, headers=HX).text
    assert f'<option value="{drop}">ACM Training Pvt Ltd' in found and f'value="{keep}"' not in found
    assert 'No company matches "zzz"' in client.get(f"/companies/{keep}/merge-candidates", params={"merge_q": "zzz"}).text
    assert ">ACM Training Pvt Ltd" in client.get(f"/companies/{keep}", params={"merge_q": "training"}).text  # without JavaScript
    resp = client.post(f"/companies/{keep}/merge", data={"drop_id": str(drop)}, follow_redirects=False)
    assert resp.status_code in (200, 303)
    with session_scope() as db:
        assert db.get(Company, drop) is None


def test_contact_merge_search_and_merge_from_the_contact_page(client: TestClient) -> None:
    with session_scope() as db:
        a = contacts.upsert(db, full_name="Vikram Shah", email="vik@one.example").id
        b = contacts.upsert(db, full_name="V. Shah", email="vshah@two.example").id
    page = client.get(f"/contacts/{a}").text
    assert 'id="contact-merge"' in page
    found = client.get(f"/contacts/{a}/merge-candidates", params={"merge_q": "vshah"}).text
    assert f'<option value="{b}">V. Shah' in found and f'value="{a}"' not in found
    resp = client.post("/contacts/merge", data={"keep_id": str(a), "drop_id": str(b), "next": f"/contacts/{a}"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith(f"/contacts/{a}")
    with session_scope() as db:
        assert {e.email for e in contacts.get_contact(db, a).email_items} == {"vik@one.example", "vshah@two.example"}

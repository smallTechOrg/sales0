"""Contacts pages (Phase 2 slice S4): searchable list, htmx search fragment, tag filter, pagination for
thousands, detail page with tags/links/timeline, duplicate merge flow, company page, escaping."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.services.crm import companies, contacts, prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry, triaged

JSON = {"Accept": "application/json"}


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _contact(**kw) -> int:  # noqa: ANN003
    with session_scope() as db:
        return contacts.upsert(db, **kw).id


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def test_empty_contacts_page_and_no_results_states(web: TestClient) -> None:
    html = _main(web.get("/contacts").text)
    assert "No contacts yet — they are created when you accept an enquiry or send an email." in html
    for hook in ('id="contacts-search"', 'id="link-duplicates"', 'id="new-contact"'):
        assert hook in html
    _contact(full_name="Priya Menon", email="priya@acme.example")
    miss = web.get("/contacts?q=zzzzz").text
    assert "No contacts match 'zzzzz'." in miss and "Clear filters" in miss
    assert "Coming in Phase 2" not in web.get("/contacts").text


def test_list_search_tag_filter_and_htmx_fragment(web: TestClient) -> None:
    a = _contact(full_name="Priya Menon", email="priya@acme.example", title="HR Head")
    b = _contact(full_name="Gina Globex", email="gina@globex.example")
    with session_scope() as db:
        contacts.add_tag(db, a, "HR head")
        contacts.add_tag(db, b, "Insurance")
    html = web.get("/contacts").text
    assert 'id="contact-list"' in html and "Priya Menon" in html and "Gina Globex" in html
    assert 'id="tag-filter"' in html and "HR head (1)" in html and "Insurance (1)" in html
    frag = web.get("/contacts?q=globex", headers=HX)
    assert frag.status_code == 200 and "<html" not in frag.text and 'id="contact-results"' in frag.text
    assert "Gina Globex" in frag.text and "Priya Menon" not in frag.text
    by_tag = web.get("/contacts?tag=Insurance").text
    assert "Gina Globex" in by_tag and "Priya Menon" not in by_tag and 'aria-current="true">Insurance' in by_tag
    api = web.get("/contacts?q=priya", headers=JSON).json()
    assert api["ok"] is True and api["data"]["total"] == 1


def test_pagination_for_thousands_of_contacts(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_CRM_PAGE_SIZE", "50")
    reset_settings()
    with session_scope() as db:
        for n in range(230):
            contacts.upsert(db, full_name=f"Bulk Person {n:04d}", email=f"bulk{n}@bulk.example")
    first = web.get("/contacts").text
    assert "Page 1 of 5" in first and "230 contacts" in first
    assert len(re.findall(r'<tr id="contact-\d+"', first)) == 50
    last = web.get("/contacts?page=5").text
    assert "Page 5 of 5" in last and len(re.findall(r'<tr id="contact-\d+"', last)) == 30
    assert "q=bulk" in web.get("/contacts?q=bulk&page=2").text.split('aria-label="Pages"')[1]
    beyond = web.get("/contacts?page=999")
    assert beyond.status_code == 200


def test_detail_page_tags_links_and_timeline(web: TestClient) -> None:
    enquiry_id = make_enquiry("P1", **triaged("genuine", "Buyer", summary="GenAI workshop"), review_status="accepted",
                              company="Acme Logistics", email="priya@acme.example", contact_person="Priya Menon")
    with session_scope() as db:
        pid = prospects.ensure_for_enquiry(db, enquiry_id).id
        cid = prospects.get_prospect(db, pid).contact_id
    assert cid is not None
    html = web.get(f"/contacts/{cid}").text
    main = _main(html)
    assert "priya@acme.example" in main and 'id="tag-form"' in main and "Linked to" in main
    assert f'href="/pipeline/prospects/{pid}"' in main  # contact-link to the prospect
    assert "contact-link" in main and 'id="timeline"' in main

    add = web.post(f"/contacts/{cid}/tags", data={"name": "HR head", "category": "role"}, follow_redirects=False)
    assert add.status_code == 303 and add.headers["location"] == f"/contacts/{cid}"
    page = web.get(f"/contacts/{cid}").text
    assert "HR head" in page and "Tag &#39;HR head&#39; added." in page or "Tag 'HR head' added." in page
    tag_id = int(re.search(r"/contacts/\d+/tags/(\d+)/delete", page).group(1))
    assert web.post(f"/contacts/{cid}/tags/{tag_id}/delete", follow_redirects=False).status_code == 303
    assert "No tags yet." in web.get(f"/contacts/{cid}").text
    # edge + error: blank and over-long tag names are rejected with a flash
    assert web.post(f"/contacts/{cid}/tags", data={"name": "  "}, follow_redirects=False).status_code == 303
    assert "flash--error" in web.get(f"/contacts/{cid}").text
    long_tag = web.post(f"/contacts/{cid}/tags", data={"name": "x" * 41}, headers=HX)
    assert "toast--error" in long_tag.text


def test_edit_contact_new_contact_and_unknown_contact(web: TestClient) -> None:
    created = web.post("/contacts", data={"full_name": "Ravi Kumar", "email": "ravi@zen.example", "company": "Zen Corp", "title": "CTO"}, follow_redirects=False)
    assert created.status_code == 303 and re.fullmatch(r"/contacts/\d+", created.headers["location"])
    url = created.headers["location"]
    page = web.get(url).text
    assert "Ravi Kumar" in page and "Zen Corp" in page and "CTO" in page
    saved = web.post(url, data={"full_name": "Ravi K. Kumar", "title": "CEO", "notes": "Met at expo", "company": "Zen Corp"}, follow_redirects=False)
    assert saved.status_code == 303
    page = web.get(url).text
    assert "Ravi K. Kumar" in page and "CEO" in page and "Met at expo" in page
    # no name / email / linkedin -> contact_invalid
    bad = web.post("/contacts", data={"title": "Nobody"}, follow_redirects=False)
    assert bad.status_code == 303 and bad.headers["location"] == "/contacts"
    assert "flash--error" in web.get("/contacts").text
    assert web.get("/contacts/99999").status_code == 404
    assert web.get("/contacts/99999", headers=JSON).json()["error"]["code"] == "not_found"


def test_duplicates_and_merge_flow(web: TestClient) -> None:
    assert "No duplicate contacts found." in web.get("/contacts/duplicates").text
    from sales0.db.models import Contact

    # Two address-book rows for the same person at the same company (identical casefolded name + company).
    with session_scope() as db:
        company_id = companies.get_or_create(db, "Acme Logistics").id
        first = Contact(full_name="Priya Menon", company_id=company_id, search_text="priya menon acme logistics")
        second = Contact(full_name="PRIYA MENON", company_id=company_id, search_text="priya menon acme logistics")
        db.add_all([first, second])
        db.flush()
        a, b = first.id, second.id
    listing = web.get("/contacts").text
    assert "Possible duplicates (1)" in listing
    page = web.get("/contacts/duplicates").text
    assert 'id="merge-list"' in page and 'id="merge-form"' in page
    assert 'hx-confirm="Merge' in page and "This cannot be undone." in page
    assert f'name="keep_id" value="{a}"' in page and f'name="drop_id" value="{b}"' in page

    self_merge = web.post("/contacts/merge", data={"keep_id": str(a), "drop_id": str(a)}, headers=HX)
    assert "Choose two different contacts to merge." in self_merge.text and "toast--error" in self_merge.text
    missing = web.post("/contacts/merge", data={"keep_id": "", "drop_id": ""}, follow_redirects=False)
    assert missing.status_code == 303 and "Choose two contacts" in web.get("/contacts/duplicates").text

    merged = web.post("/contacts/merge", data={"keep_id": str(a), "drop_id": str(b)}, headers=HX)
    assert merged.status_code == 204 and merged.headers["hx-redirect"] == "/contacts/duplicates"
    after = web.get("/contacts/duplicates").text
    assert "No duplicate contacts found." in after and "Merged into" in after
    old = web.get(f"/contacts/{b}", follow_redirects=False)  # a merged-away link lands on the survivor
    assert old.status_code == 303 and old.headers["location"] == f"/contacts/{a}"
    assert web.get("/contacts/987654").status_code == 404
    assert "contact merged" in web.get(f"/contacts/{a}").text.lower() or "merged" in web.get(f"/contacts/{a}").text.lower()


def test_company_page_rolls_up_with_timeline(web: TestClient) -> None:
    enquiry_id = make_enquiry("P1", **triaged("genuine", "Buyer", summary="GenAI workshop"), review_status="accepted",
                              company="Acme Logistics", email="priya@acme.example", contact_person="Priya Menon")
    with session_scope() as db:
        view = prospects.ensure_for_enquiry(db, enquiry_id)
        company_id = view.company_id
    assert company_id is not None
    html = web.get(f"/companies/{company_id}").text
    main = _main(html)
    assert "Acme Logistics" in main and 'id="company-header"' in main and 'id="timeline-card"' in main
    assert 'id="company-people"' in main and "priya@acme.example" in main  # the page lists the people with their e-mail
    assert f'action="/companies/{company_id}"' in main  # company details are editable (CRM feedback B3)
    filtered = web.get(f"/contacts?company_id={company_id}").text
    assert "Company: <strong>Acme Logistics</strong>" in filtered and "Priya Menon" in filtered
    assert web.get("/companies/99999").status_code == 404


def test_contact_text_is_escaped(web: TestClient) -> None:
    script = "<script>alert(1)</script>"
    cid = _contact(full_name=f"Mallory {script}", email="mallory@evil.example", title=f"Boss {script}")
    with session_scope() as db:
        contacts.add_tag(db, cid, f"tag{script}"[:40])
    for path in ("/contacts", f"/contacts/{cid}", "/contacts?q=mallory"):
        html = web.get(path).text
        assert script not in html, path
        assert "&lt;script&gt;" in html, path

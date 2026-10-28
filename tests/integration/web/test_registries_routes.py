"""Competitors & Groups registries (Phase 2 slice S4): CRUD for app-owned rows, read-only badges for
sheet-fed rows, delete protection, competitor detail with linked clients, escaping."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import utcnow
from sales0.db.engine import session_scope
from sales0.db.models import ChannelGroup, Competitor, SheetSource
from sales0.services.crm import clients, registries
from sales0.web.app import create_app
from tests.integration.web.conftest import HX

JSON = {"Accept": "application/json"}


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _competitor_id(name: str) -> int:
    with session_scope() as db:
        return db.scalars(select(Competitor).where(Competitor.name == name)).one().id


def _make_sheet_fed(target_type: str, target_id: int, entity: str) -> None:
    with session_scope() as db:
        db.add(
            SheetSource(
                entity=entity, key=f"k-{target_type}-{target_id}", target_type=target_type, target_id=target_id,
                sheet_id="sheet-b", gid="1", tab_name="Competitors", row_number=3, row_hash="h", fields={},
                first_seen_at=utcnow(), last_seen_at=utcnow(),
            )
        )  # fmt: skip


def test_page_lists_the_seeded_competitors_and_empty_groups(web: TestClient) -> None:
    html = web.get("/competitors").text
    main = _main(html)
    assert "Competitors &amp; Groups" in html and "Coming in Phase 2" not in html
    for hook in ('id="competitor-list"', 'id="groups-empty"', 'id="new-competitor"', 'id="new-group"'):
        assert hook in main, hook
    assert "Future Labs" in main  # seeded by the migration
    assert "No groups yet." in main


def test_competitor_crud_round_trip(web: TestClient) -> None:
    created = web.post(
        "/competitors/competitor",
        data={"name": "Rival Academy", "website": "https://rival.example", "approx_clients": "40", "client_names": "A, B", "contact": "Sam", "email": "sam@rival.example", "notes": "Aggressive pricing"},
        follow_redirects=False,
    )  # fmt: skip
    assert created.status_code == 303 and created.headers["location"] == "/competitors"
    page = web.get("/competitors").text
    assert "Rival Academy" in page and "https://rival.example" in page and "Added competitor Rival Academy." in page
    cid = _competitor_id("Rival Academy")
    updated = web.post(
        f"/competitors/competitor/{cid}",
        data={"name": "Rival Academy Pvt", "website": "https://rival.example", "approx_clients": "55", "notes": "Changed", "active": ["false", "true"]},
        follow_redirects=False,
    )  # fmt: skip
    assert updated.status_code == 303
    row = _main(web.get("/competitors").text)
    assert "Rival Academy Pvt" in row and ">55<" in row
    off = web.post(f"/competitors/competitor/{cid}", data={"name": "Rival Academy Pvt", "active": ["false"]}, headers=HX)
    assert off.status_code == 204
    assert re.search(rf'<tr id="competitor-{cid}" data-active="0"', web.get("/competitors").text)
    deleted = web.post(f"/competitors/competitor/{cid}/delete", headers=HX)
    assert deleted.status_code == 204
    assert "Rival Academy Pvt" not in web.get("/competitors").text


def test_competitor_validation_errors(web: TestClient) -> None:
    blank = web.post("/competitors/competitor", data={"name": "  "}, follow_redirects=False)
    assert blank.status_code == 303
    assert "A competitor needs a name." in web.get("/competitors").text
    htmx = web.post("/competitors/competitor", data={"name": ""}, headers=HX)
    assert "A competitor needs a name." in htmx.text and "toast--error" in htmx.text
    bad_number = web.post("/competitors/competitor", data={"name": "Num Co", "approx_clients": "many"}, headers=HX)
    assert "toast--error" in bad_number.text
    assert web.post("/competitors/competitor/99999/delete", headers=JSON).status_code == 404
    api = web.post("/competitors/competitor", data={"name": ""}, headers=JSON)
    assert api.status_code == 400 and api.json()["error"]["code"] == "competitor_invalid"


def test_sheet_fed_rows_are_read_only_with_a_badge_and_cannot_be_deleted(web: TestClient) -> None:
    web.post("/competitors/competitor", data={"name": "Sheet Rival", "website": "https://sheet-rival.example", "notes": "mine"}, follow_redirects=False)
    cid = _competitor_id("Sheet Rival")
    _make_sheet_fed("competitor", cid, "competitor")
    html = _main(web.get("/competitors").text)
    row = html.split(f'id="competitor-{cid}"', 1)[1].split("</tr>", 1)[0]
    assert "Synced from sheet" in row and 'readonly aria-readonly="true"' in row
    assert re.search(r'<button[^>]*disabled[^>]*>Delete</button>', row)
    assert "/delete" not in row
    # API refuses the delete
    resp = web.post(f"/competitors/competitor/{cid}/delete", headers=JSON)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "sheet_owned"
    flash = web.post(f"/competitors/competitor/{cid}/delete", follow_redirects=False)
    assert flash.status_code == 303 and "flash--error" in web.get("/competitors").text
    # sheet-owned fields in the form are ignored; notes and Active are saved
    web.post(f"/competitors/competitor/{cid}", data={"name": "HACKED", "website": "https://hacked.example", "notes": "internal note", "active": ["false"]}, follow_redirects=False)
    with session_scope() as db:
        got = registries.get_competitor(db, cid)
        assert got.name == "Sheet Rival" and got.website == "https://sheet-rival.example"
        assert got.notes == "internal note" and got.active is False


def test_group_crud_and_sheet_fed_group(web: TestClient) -> None:
    created = web.post("/competitors/group", data={"name": "HR Leaders WhatsApp", "platform": "whatsapp", "link": "https://chat.whatsapp.com/abc", "details": "Keep"}, follow_redirects=False)
    assert created.status_code == 303
    page = _main(web.get("/competitors").text)
    assert 'id="group-list"' in page and "HR Leaders WhatsApp" in page and "Whatsapp" in page
    with session_scope() as db:
        group = db.scalars(select(ChannelGroup).where(ChannelGroup.name == "HR Leaders WhatsApp")).one()
        gid = group.id
    toggled = web.post(f"/competitors/group/{gid}", data={"name": "HR Leaders WhatsApp", "active": "false"}, headers=HX)
    assert toggled.status_code == 204
    assert re.search(rf'<tr id="group-{gid}" data-active="0"', web.get("/competitors").text)
    assert "Inactive - turn on" in web.get("/competitors").text
    renamed = web.post(f"/competitors/group/{gid}", data={"name": "HR Heads", "platform": "linkedin", "link": "", "details": "", "active": ["false", "true"]}, follow_redirects=False)
    assert renamed.status_code == 303
    html = web.get("/competitors").text
    assert "HR Heads" in html and "Linkedin" in html
    _make_sheet_fed("channel_group", gid, "group")
    sheet_row = web.get("/competitors").text.split(f'id="group-{gid}"', 1)[1].split("</tr>", 1)[0]
    assert "Synced from sheet" in sheet_row and "disabled" in sheet_row
    resp = web.post(f"/competitors/group/{gid}/delete", headers=JSON)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "sheet_owned"
    # group with no name
    bad = web.post("/competitors/group", data={"name": " "}, headers=HX)
    assert "toast--error" in bad.text
    with session_scope() as db:
        db.query(SheetSource).delete()
    assert web.post(f"/competitors/group/{gid}/delete", headers=HX).status_code == 204
    assert "No groups yet." in web.get("/competitors").text


def test_competitor_detail_links_a_client(web: TestClient) -> None:
    web.post("/competitors/competitor", data={"name": "Link Rival"}, follow_redirects=False)
    cid = _competitor_id("Link Rival")
    with session_scope() as db:
        client_id = clients.create_legacy(db, name="Linked Client Ltd").id
    page = web.get(f"/competitors/competitor/{cid}")
    assert page.status_code == 200 and 'id="competitor-enquiries"' in page.text and 'id="link-client-form"' in page.text
    assert "No enquiry mentions this competitor yet." in page.text and "Linked Client Ltd" in page.text
    linked = web.post(f"/competitors/competitor/{cid}/link", data={"entity_type": "client", "entity_id": str(client_id)}, follow_redirects=False)
    assert linked.status_code == 303
    after = web.get(f"/competitors/competitor/{cid}").text
    assert f'href="/pipeline/clients/{client_id}"' in after and "Linked the client." in after
    assert "<option value=\"%d\">" % client_id not in after  # no longer offered
    assert web.get(f"/competitors/competitor/{cid}", headers=JSON).json()["data"]["id"] == cid
    bad = web.post(f"/competitors/competitor/{cid}/link", data={"entity_type": "company", "entity_id": "1"}, headers=HX)
    assert "toast--error" in bad.text
    assert web.get("/competitors/competitor/99999").status_code == 404
    assert web.get("/competitors").text.count("Link Rival") >= 1


def test_registry_text_is_escaped(web: TestClient) -> None:
    script = "<script>alert(1)</script>"
    web.post("/competitors/competitor", data={"name": f"Evil {script}", "notes": script, "client_names": script}, follow_redirects=False)
    web.post("/competitors/group", data={"name": f"G {script}", "details": script}, follow_redirects=False)
    cid = _competitor_id(f"Evil {script}")
    for path in ("/competitors", f"/competitors/competitor/{cid}"):
        html = web.get(path).text
        assert script not in html, path
        assert "&lt;script&gt;" in html, path

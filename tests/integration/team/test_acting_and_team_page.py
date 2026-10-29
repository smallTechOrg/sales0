"""Acting-as cookie, header selector and the Team page permissions."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import TeamMember
from tests.integration.team.conftest import ADMIN_EMAIL


def _add(client: TestClient, name: str, email: str, role: str = "member") -> int:
    resp = client.post("/team/members", data={"name": name, "email": email, "role": role}, follow_redirects=True)
    assert resp.status_code == 200
    with session_scope() as db:
        return db.execute(select(TeamMember).where(TeamMember.email == email.lower())).scalar_one().id


def test_admin_is_seeded_and_default_acting_member(client: TestClient) -> None:
    html = client.get("/team").text
    assert ADMIN_EMAIL in html and "You (acting)" in html and "Ada Admin (admin)" in html
    assert 'id="acting-select"' in html and "Connect my Gmail" in html and "Add a team member" in html


def test_header_selector_appears_on_every_page(client: TestClient) -> None:
    for path in ("/inbox", "/runs", "/setup", "/team"):
        assert 'id="acting-select"' in client.get(path).text, path


def test_switching_sets_a_signed_cookie_and_changes_the_view(client: TestClient) -> None:
    bob = _add(client, "Bob", "Bob@Team.Example")
    resp = client.post("/team/acting", data={"member_id": bob, "next": "/runs"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/runs"
    cookie = resp.headers["set-cookie"]
    assert "sales0_acting=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    page = client.get("/team").text
    assert "Add a team member" not in page  # Bob is a plain member
    assert f'<option value="{bob}" selected>' in page


def test_tampered_cookie_falls_back_to_the_admin(client: TestClient) -> None:
    bob = _add(client, "Bob", "bob@team.example")
    client.cookies.set("sales0_acting", f"{bob}.forged")
    page = client.get("/team").text
    assert "Add a team member" in page  # admin, not Bob


def test_open_redirect_in_next_is_neutralised(client: TestClient) -> None:
    with session_scope() as db:
        admin = db.execute(select(TeamMember)).scalars().first()
        assert admin is not None
    for evil in ("https://evil.example/", "//evil.example", "/\\evil.example"):
        resp = client.post("/team/acting", data={"member_id": admin.id, "next": evil}, follow_redirects=False)
        assert resp.headers["location"] == "/team"


def test_inactive_or_unknown_member_cannot_be_selected(client: TestClient) -> None:
    bob = _add(client, "Bob", "bob@team.example")
    assert client.post("/team/members/%d/active" % bob, data={"active": "0"}).status_code == 200
    assert client.post("/team/acting", data={"member_id": bob}).status_code == 404
    assert client.post("/team/acting", data={"member_id": 9999}).status_code == 404


def test_members_cannot_add_or_deactivate_members(client: TestClient) -> None:
    bob = _add(client, "Bob", "bob@team.example")
    client.post("/team/acting", data={"member_id": bob})
    assert client.post("/team/members", data={"name": "X", "email": "x@team.example"}).status_code == 403
    assert client.post("/team/members/1/active", data={"active": "0"}).status_code == 403
    assert client.post("/team/gmail/disconnect", data={"member_id": 1}).status_code == 403


def test_duplicate_and_invalid_members_show_a_clear_error(client: TestClient) -> None:
    assert client.post("/team/members", data={"name": "Dup", "email": ADMIN_EMAIL}).status_code == 409
    assert client.post("/team/members", data={"name": "Bad", "email": "nope"}).status_code == 400


def test_restart_keeps_members_and_acting_choice(client: TestClient) -> None:
    from sales0.web.app import create_app

    bob = _add(client, "Bob", "bob@team.example")
    client.post("/team/acting", data={"member_id": bob})
    cookie = client.cookies.get("sales0_acting")
    with TestClient(create_app(), base_url="http://127.0.0.1:8766") as again:  # second startup: seed is idempotent
        again.cookies.set("sales0_acting", cookie)
        page = again.get("/team").text
    assert page.count("bob@team.example") >= 1 and f'<option value="{bob}" selected>' in page
    with session_scope() as db:
        assert len(db.execute(select(TeamMember)).scalars().all()) == 2

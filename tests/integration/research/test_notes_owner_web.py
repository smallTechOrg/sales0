"""Logged-in members: a note is edited / deleted only by its author; research and summary are open to members."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import ProspectNote
from tests.integration.members.conftest import activate, add_member
from tests.integration.members.conftest import admin, env, make_client  # noqa: F401 - fixtures
from tests.integration.research.conftest import make_prospect

JSON = {"Accept": "application/json"}


def test_only_the_author_changes_a_note(admin: TestClient, make_client: Callable[[], TestClient]) -> None:  # noqa: F811
    pid = make_prospect(website=None, linkedin=None)
    link = add_member(admin, "Ravi Kumar", "ravi@acct.example")
    ravi = activate(make_client, link, "ravi@acct.example")
    r = ravi.post(f"/pipeline/prospects/{pid}/notes", data={"body": "Ravi's call notes"}, headers=JSON)
    assert r.status_code == 200 and r.json()["data"]["author"] == "Ravi Kumar"
    with session_scope() as db:
        nid = db.scalar(select(ProspectNote.id))
    page = admin.get(f"/pipeline/prospects/{pid}").text
    assert "Ravi's call notes" in page.replace("&#39;", "'") and f"/pipeline/notes/{nid}/edit" not in page  # no edit form for others
    denied = admin.post(f"/pipeline/notes/{nid}/edit", data={"body": "admin rewrite"}, headers=JSON)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "not_owner"
    assert admin.post(f"/pipeline/notes/{nid}/delete", headers=JSON).status_code == 403
    assert f"/pipeline/notes/{nid}/edit" in ravi.get(f"/pipeline/prospects/{pid}").text
    assert ravi.post(f"/pipeline/notes/{nid}/delete", headers=JSON).status_code == 200

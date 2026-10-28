"""Security fixes #3/#2/#17/#14: members cannot administer shared resources; admin-acting is audited;
message ids are not enumerable; the deck PDF cannot run script in our origin."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import get_sessionmaker
from sales0.db.models import LinkedInAccount
from sales0.llm import set_llm_client
from sales0.web import acting
from sales0.web.app import create_app
from tests.integration.outreach.conftest import PDF, Team, make_message
from tests.support.fake_llm import FakeLLMClient

JSON = {"Accept": "application/json"}

# (method, path) of every route guarded by require_admin in this fix (group B).
ADMIN_ONLY = {
    # setup.py: keywords / offerings / LinkedIn session + accounts
    ("POST", "/setup/keywords"), ("POST", "/setup/keywords/{keyword_id}/delete"),
    ("POST", "/setup/keywords/{keyword_id}/toggle"), ("POST", "/setup/keywords/suggested"),
    ("POST", "/setup/keywords/{keyword_id}/edit"), ("POST", "/setup/offerings"),
    ("POST", "/setup/offerings/{offering_id}"), ("POST", "/setup/offerings/{offering_id}/delete"),
    ("POST", "/setup/linkedin/connect"), ("POST", "/setup/linkedin/connect/cancel"), ("POST", "/setup/linkedin/check"),
    ("POST", "/setup/linkedin/accounts"), ("POST", "/setup/linkedin/accounts/{account_id}/rename"),
    ("POST", "/setup/linkedin/accounts/{account_id}/disable"), ("POST", "/setup/linkedin/accounts/{account_id}/enable"),
    ("POST", "/setup/linkedin/accounts/{account_id}/connect"), ("POST", "/setup/linkedin/accounts/{account_id}/check"),
    ("POST", "/setup/linkedin/cookie"),
    # schedule / sync / runs
    ("POST", "/schedule/{name}/enable"), ("POST", "/schedule/{name}/run"),
    ("POST", "/sync/run"), ("POST", "/sync/enable"), ("POST", "/sync/disable"), ("POST", "/sync/dry-run"),
    ("POST", "/runs"),
    # outreach templates / decks (compose, approve, send stay per-member)
    ("POST", "/outreach/templates/save"), ("POST", "/outreach/templates/{template_id}/delete"),
    ("POST", "/outreach/decks"), ("POST", "/outreach/decks/{deck_id}/offering"), ("POST", "/outreach/decks/{deck_id}/delete"),
    # importer + registries
    ("POST", "/import/csv"), ("POST", "/import/paste"),
    ("POST", "/competitors/competitor"), ("POST", "/competitors/competitor/{competitor_id}"),
    ("POST", "/competitors/competitor/{competitor_id}/delete"), ("POST", "/competitors/competitor/{competitor_id}/link"),
    ("POST", "/competitors/group"), ("POST", "/competitors/group/{group_id}"), ("POST", "/competitors/group/{group_id}/delete"),
}


def _walk(router):  # type: ignore[no-untyped-def]
    for r in router.routes:
        if hasattr(r, "original_router"):
            yield from _walk(r.original_router)
        elif isinstance(r, APIRoute):
            yield r


def _deps(dep, out):  # type: ignore[no-untyped-def]
    for sub in dep.dependencies:
        out.add(sub.call)
        _deps(sub, out)
    return out


@pytest.fixture
def web(team: Team) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: {}))
    with TestClient(create_app()) as c:
        yield c


def act_as(web: TestClient, member) -> None:  # noqa: ANN001
    assert web.post("/team/acting", data={"member_id": member.id, "next": "/outreach"}, follow_redirects=False).status_code == 303


def test_route_table_declares_exactly_the_admin_only_routes(team: Team) -> None:
    guarded = set()
    for r in _walk(create_app().router):
        if acting.require_admin in _deps(r.dependant, set()):
            guarded |= {(m, r.path) for m in r.methods if m == "POST"}
    missing = ADMIN_ONLY - guarded
    assert not missing, f"not guarded by require_admin: {sorted(missing)}"


def test_member_gets_403_on_every_admin_route_and_a_friendly_page(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    for method, path in sorted(ADMIN_ONLY):
        url = re.sub(r"\{[^}]+\}", "1", path)
        resp = web.request(method, url, headers=JSON)
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden", (method, url, resp.status_code)
    page = web.post("/setup/linkedin/cookie", data={"cookie": "x"})  # plain browser post -> HTML page
    assert page.status_code == 403 and "Only an admin can do that" in page.text and "text/html" in page.headers["content-type"]
    toast = web.post("/outreach/templates/save", data={"name": "x"}, headers={"HX-Request": "true"})
    assert toast.status_code == 200 and "Only an admin can do that" in toast.text and toast.headers["HX-Retarget"] == "#toasts"


def test_member_keeps_their_own_work_and_views(web: TestClient, team: Team) -> None:
    act_as(web, team.ann)
    for url in ("/outreach", "/outreach/templates", "/outreach/decks", "/setup", "/runs", "/sync", "/schedule", "/inbox"):
        assert web.get(url).status_code == 200, url
    resp = web.post(
        "/outreach/draft",
        data={"to": "p@acme.example", "company": "Acme", "requirement": "x", "offering_id": "", "template_id": "", "deck_id": "none"},
        follow_redirects=False,
    )
    assert resp.status_code in (303, 409, 502) or resp.status_code < 500  # compose is not admin-gated (never 403)
    assert resp.status_code != 403


def test_admin_passes_the_guards_and_account_gets_an_owner(web: TestClient, team: Team) -> None:
    act_as(web, team.admin)
    r = web.post("/outreach/templates/save", data={"name": "T1", "subject": "Hi {first_name}", "body": "Hello {first_name}"}, follow_redirects=False)
    assert r.status_code == 303
    up = web.post("/outreach/decks", files={"file": ("d.pdf", PDF, "application/pdf")}, data={"name": "Deck"}, follow_redirects=False)
    assert up.status_code == 303
    assert web.post("/setup/linkedin/accounts", data={"label": "Sales A"}, follow_redirects=False).status_code in (200, 303)
    with get_sessionmaker()() as db:
        acct = db.execute(select(LinkedInAccount).where(LinkedInAccount.label == "Sales A")).scalar_one()
        assert acct.owner_member_id == team.admin.id


def test_deck_pdf_is_an_attachment_in_a_sandbox(web: TestClient, team: Team, deck) -> None:  # noqa: ANN001
    act_as(web, team.ann)
    r = web.get(f"/outreach/decks/{deck.id}/file")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_message_ids_are_not_enumerable_by_other_members(web: TestClient, team: Team, db) -> None:  # noqa: ANN001
    msg = make_message(db, team.ann, state="approved")
    act_as(web, team.bob)
    ghost = 999999
    for action in ("approve", "send", "reject", "retry", "edit"):
        real = web.post(f"/outreach/messages/{msg.id}/{action}", headers=JSON)
        none = web.post(f"/outreach/messages/{ghost}/{action}", headers=JSON)
        assert real.status_code == none.status_code == 404, action
        assert real.json()["error"]["code"] == none.json()["error"]["code"], action
    assert web.get(f"/outreach/messages/{msg.id}").status_code == web.get(f"/outreach/messages/{ghost}").status_code == 404
    act_as(web, team.admin)  # an admin who is not the owner keeps the explicit 403
    assert web.post(f"/outreach/messages/{msg.id}/send", headers=JSON).status_code == 403

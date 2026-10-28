"""QA hardening: compose prefill precedence, spoof-proof login throttle, bounded ids, registries
validation, upload/paste caps, link guards, merge confirm text, logout contrast."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from html import unescape

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Contact, Enquiry, LinkedinProfile, Prospect
from sales0.services.crm import companies, prospects
from sales0.web.app import UVICORN_OPTIONS, create_app
from tests.integration.web.conftest import _free_port, make_enquiry

JSON = {"Accept": "application/json"}


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


# ---------------------------------------------------------------------- 1. prefill
def test_compose_keeps_prospect_contact_email_when_newest_enquiry_has_none(web: TestClient) -> None:
    eid = make_enquiry("P1", email="priya@acme.example", company="Acme Logistics", requirement_summary="GenAI workshop")
    with session_scope() as db:
        pid = prospects.ensure_for_enquiry(db, eid).id
    with session_scope() as db:
        db.get(Enquiry, eid).email = None  # the enquiry itself has no email; the prospect's contact does
    html = unescape(web.get(f"/outreach/compose?prospect_id={pid}").text)
    assert 'value="priya@acme.example"' in html


# ---------------------------------------------------------------------- 2. throttle vs X-Forwarded-For
def test_rotating_x_forwarded_for_still_locks_out(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_APP_PASSWORD", "right-pass")
    monkeypatch.setenv("SALES0_SECRET_KEY", "k")
    monkeypatch.setenv("SALES0_LOGIN_MAX_ATTEMPTS", "3")
    reset_settings()
    port = _free_port()
    assert UVICORN_OPTIONS["proxy_headers"] is False
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", **UVICORN_OPTIONS)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive()
        time.sleep(0.05)
    try:
        statuses = []
        for i in range(5):
            r = httpx.post(
                f"http://127.0.0.1:{port}/login", data={"password": "wrong"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}
            )
            statuses.append(r.status_code)
        assert statuses[:3] == [401, 401, 401] and statuses[3:] == [429, 429]
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        reset_settings()


# ---------------------------------------------------------------------- 3. bounded ids
HUGE = "99999999999999999999"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", f"/contacts/{HUGE}"),
        ("get", f"/companies/{HUGE}"),
        ("get", f"/pipeline/prospects/{HUGE}"),
        ("get", f"/pipeline/leads/{HUGE}"),
        ("get", f"/pipeline/clients/{HUGE}"),
        ("get", f"/competitors/competitor/{HUGE}"),
        ("get", f"/import/batches/{HUGE}"),
        ("get", f"/outreach/messages/{HUGE}"),
        ("get", f"/digest/{HUGE}"),
        ("post", f"/pipeline/prospects/{HUGE}/note"),
        ("post", f"/contacts/{HUGE}/tags/{HUGE}/delete"),
        ("get", "/contacts/0"),
        ("get", "/contacts/-3"),
    ],
)
def test_huge_path_ids_are_client_errors_not_500(web: TestClient, method: str, path: str) -> None:
    r = getattr(web, method)(path, headers=JSON, **({"data": {"body": "x"}} if method == "post" else {}))
    assert r.status_code in (404, 422), (path, r.status_code)
    assert getattr(web, method)(path).status_code in (404, 422)


def test_huge_query_ids_are_client_errors(web: TestClient) -> None:
    assert web.get(f"/outreach/compose?prospect_id={HUGE}").status_code == 400
    assert web.get(f"/outreach/compose?enquiry_id={HUGE}").status_code == 400
    assert web.get(f"/contacts?page={HUGE}").status_code == 200


# ---------------------------------------------------------------------- 4. registries validation
def test_competitor_validation_rejects_long_names_and_negative_counts(web: TestClient) -> None:
    long_name = web.post("/competitors/competitor", data={"name": "x" * 500}, headers=JSON)
    assert long_name.status_code == 400 and long_name.json()["error"]["code"] == "competitor_invalid"
    negative = web.post("/competitors/competitor", data={"name": "Neg Co", "approx_clients": "-5"}, headers=JSON)
    assert negative.status_code == 400 and "0 or more" in negative.json()["error"]["message"]
    huge = web.post("/competitors/competitor", data={"name": "Big Co", "approx_clients": HUGE}, headers=JSON)
    assert huge.status_code == 400
    long_site = web.post("/competitors/competitor", data={"name": "Site Co", "website": "http://" + "a" * 600}, headers=JSON)
    assert long_site.status_code == 400
    form = web.post("/competitors/competitor", data={"name": "y" * 500}, follow_redirects=False)
    assert form.status_code == 303  # browser form: flash + redirect, not a 500
    assert "too long" in web.get("/competitors").text
    ok = web.post("/competitors/competitor", data={"name": "Fine Co", "approx_clients": "0"}, headers=JSON)
    assert ok.status_code == 201


def test_group_validation_rejects_long_fields(web: TestClient) -> None:
    r = web.post("/competitors/group", data={"name": "g" * 500}, headers=JSON)
    assert r.status_code == 400 and r.json()["error"]["code"] == "group_invalid"
    r = web.post("/competitors/group", data={"name": "ok", "link": "http://" + "a" * 600}, headers=JSON)
    assert r.status_code == 400


# ---------------------------------------------------------------------- 5. caps
def test_paste_over_cap_is_rejected_without_calling_the_llm(web: TestClient) -> None:
    r = web.post("/import/paste", data={"text": "a" * 20_001}, headers=JSON)
    assert r.status_code == 413 and r.json()["error"]["code"] == "import_too_large"


def test_csv_upload_over_5mb_is_413_before_parsing(web: TestClient) -> None:
    body = b"x" * (5 * 1024 * 1024 + 200_000)
    r = web.post("/import/csv", data={"kind": "contacts"}, files={"file": ("big.csv", body, "text/csv")}, headers=JSON)
    assert r.status_code == 413 and r.json()["error"]["code"] == "import_too_large"
    small = web.post("/import/csv", data={"kind": "contacts"}, files={"file": ("a.csv", b"name,email\nA,a@x.example\n", "text/csv")}, headers=JSON)
    assert small.status_code == 201


# ---------------------------------------------------------------------- 6. href guards
def test_non_http_linkedin_url_is_not_rendered_as_a_link(web: TestClient) -> None:
    with session_scope() as db:
        pid = prospects.create_manual(db, name="Zed Link", linkedin_url="https://www.linkedin.com/in/zed").id

    def set_url(url: str) -> None:
        with session_scope() as db:
            prospect = db.get(Prospect, pid)
            assert prospect is not None and prospect.linkedin_profile_id is not None
            profile = db.get(LinkedinProfile, prospect.linkedin_profile_id)
            assert profile is not None
            profile.profile_url = url

    set_url("javascript:alert(1)")
    assert 'href="javascript:' not in web.get(f"/pipeline/prospects/{pid}").text
    set_url("https://www.linkedin.com/in/x")
    assert 'href="https://www.linkedin.com/in/x"' in web.get(f"/pipeline/prospects/{pid}").text


# ---------------------------------------------------------------------- 7. UX
def test_merge_confirm_names_ids_and_emails_for_equal_names(web: TestClient) -> None:
    with session_scope() as db:
        company_id = companies.get_or_create(db, "Acme Logistics").id
        a = Contact(full_name="Priya Menon", email="p1@acme.example", company_id=company_id, search_text="priya menon acme logistics")
        b = Contact(full_name="Priya Menon", email="p2@acme.example", company_id=company_id, search_text="priya menon acme logistics")
        db.add_all([a, b])
        db.flush()
        ids = (a.id, b.id)
    page = unescape(web.get("/contacts/duplicates").text)
    assert f"(#{ids[0]}, p1@acme.example)" in page and f"(#{ids[1]}, p2@acme.example)" in page


def test_logout_link_has_light_text_on_the_dark_header(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_APP_PASSWORD", "pw-pw-pw")
    monkeypatch.setenv("SALES0_SECRET_KEY", "k")
    reset_settings()
    try:
        with TestClient(create_app(), follow_redirects=False) as c:
            c.post("/login", data={"password": "pw-pw-pw"})
            assert 'id="logout"' in c.get("/inbox").text
            css = c.get("/static/css/app.css").text
            assert ".site-header .logout-form .btn" in css and "var(--color-header-text)" in css
    finally:
        reset_settings()

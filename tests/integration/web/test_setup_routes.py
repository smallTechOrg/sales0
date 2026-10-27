"""Setup page (slice F): keywords, offerings, LinkedIn card, safety caps — through the HTTP
routes on a real migrated SQLite file."""

from __future__ import annotations

import re
import threading
from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Keyword, Offering
from sales0.jobs.runner import get_runner
from tests.integration.web.conftest import HX, make_enquiry, set_session, triaged


def _keyword_count() -> int:
    with session_scope() as db:
        return int(db.scalar(select(func.count()).select_from(Keyword)) or 0)


def _is_toast_error(resp, text: str) -> bool:  # noqa: ANN001
    return (
        resp.status_code == 200
        and resp.headers.get("HX-Retarget") == "#toasts"
        and "toast--error" in resp.text
        and text in unescape(resp.text)
    )


def _offering_id(name: str) -> int:
    with session_scope() as db:
        row = db.scalar(select(Offering).where(Offering.name == name))
        assert row is not None
        return row.id


def test_setup_page_lists_seeded_offerings_by_group_and_empty_keywords(client: TestClient) -> None:
    html = unescape(client.get("/setup").text)
    offering_list = html.split('id="offering-list"', 1)[1].split("</datalist>", 1)[0]
    headings = re.findall(r'<h3 class="group-heading">([^<]+)</h3>', offering_list)
    assert headings == ["Training", "Services", "Products"]
    names = re.findall(r"<strong>([^<]+?)(?: <span|</strong>)", offering_list)
    assert names == ["AI corporate workshops", "Technical AI training", "AI consulting & build services", "Products"]
    assert "No keywords yet — add 10–20 phrases buyers use, e.g. 'corporate AI training'." in html
    assert 'id="keyword-input"' in html and 'id="keyword-list"' in html
    assert "Change these in .env" in html and "Posts per day" in html and "150" in html
    card = html.split('id="linkedin-card"', 1)[1].split("</section>", 1)[0]
    assert "sales0 uses your team's own LinkedIn login in a dedicated browser profile on this computer." in card
    assert 'id="connect-linkedin"' in card and 'id="check-linkedin"' in card
    assert "every 1s" not in card  # idle: no polling


def test_add_keyword_normalises_whitespace_and_rejects_case_insensitive_duplicate(client: TestClient) -> None:
    resp = client.post("/setup/keywords", data={"text": "  Corporate   AI training "}, headers=HX)
    assert resp.status_code == 200
    body = unescape(resp.text)
    assert resp.text.lstrip().startswith('<div id="keyword-list">')
    assert "<strong>Corporate AI training</strong>" in body
    assert 'hx-swap-oob="beforeend"' in body and "Added keyword 'Corporate AI training'." in body

    dup = client.post("/setup/keywords", data={"text": "corporate ai training"}, headers=HX)
    assert _is_toast_error(dup, "'Corporate AI training' is already in your keywords.")
    assert _keyword_count() == 1


def test_add_keyword_too_short_shows_keyword_invalid_toast(client: TestClient) -> None:
    resp = client.post("/setup/keywords", data={"text": "a"}, headers=HX)
    assert _is_toast_error(resp, "Keyword must be 2–100 characters.")
    missing = client.post("/setup/keywords", data={}, headers=HX)
    assert _is_toast_error(missing, "Keyword must be 2–100 characters.")
    assert _keyword_count() == 0


def test_keyword_survives_reload_and_non_htmx_post_redirects(client: TestClient) -> None:
    resp = client.post("/setup/keywords", data={"text": "ai workshop"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    html = unescape(client.get("/setup").text)
    assert "<strong>ai workshop</strong>" in html and "Not run yet" in html


def test_toggle_and_remove_keyword(client: TestClient) -> None:
    client.post("/setup/keywords", data={"text": "ai consulting"}, headers=HX)
    with session_scope() as db:
        kid = db.scalar(select(Keyword.id))
    row = unescape(client.post(f"/setup/keywords/{kid}/toggle", data={"active": "false"}, headers=HX).text)
    assert row.lstrip().startswith(f'<li id="keyword-{kid}" data-active="false"')
    assert 'aria-checked="false"' in row and "Paused" in row
    assert "Remove keyword 'ai consulting'?" in row  # hx-confirm text
    with session_scope() as db:
        assert db.get(Keyword, kid).active is False  # type: ignore[union-attr]

    removed = client.post(f"/setup/keywords/{kid}/delete", headers=HX)
    assert "No keywords yet" in removed.text and "Its run history is kept." in removed.text
    assert _keyword_count() == 0
    again = client.post(f"/setup/keywords/{kid}/delete", headers=HX)
    assert _is_toast_error(again, "That item no longer exists — refresh the page.")


def test_create_offering_in_new_group_then_edit_it(client: TestClient) -> None:
    resp = client.post(
        "/setup/offerings", data={"name": "AI audits", "group_name": "Services+", "description": "Audit AI usage"}, headers=HX
    )
    created = unescape(resp.text)
    assert resp.status_code == 200 and resp.text.lstrip().startswith('<div id="offering-list"')
    assert '<h3 class="group-heading">Services+</h3>' in created and "<strong>AI audits" in created
    assert '<option value="Services+">' in created  # datalist refreshed with the new group
    assert "Added offering 'AI audits' to Services+." in created

    oid = _offering_id("AI audits")
    edit = client.get(f"/setup/offerings/{oid}/edit", headers=HX).text
    assert f'<li id="offering-{oid}">' in edit and 'value="AI audits"' in edit and "Save changes" in edit
    saved = client.post(
        f"/setup/offerings/{oid}", data={"name": "AI readiness audits", "group_name": "Services+", "description": "x"}, headers=HX
    ).text
    assert "<strong>AI readiness audits" in saved and "<strong>AI audits" not in saved


def test_offering_duplicate_and_invalid_inputs_show_toasts(client: TestClient) -> None:
    dup = client.post("/setup/offerings", data={"name": "products", "group_name": "Products"}, headers=HX)
    assert _is_toast_error(dup, "An offering named 'Products' already exists.")
    blank = client.post("/setup/offerings", data={"name": "  ", "group_name": "Training"}, headers=HX)
    assert _is_toast_error(blank, "Offering name (1–80) and group (1–60) are required.")
    too_long = client.post("/setup/offerings", data={"name": "x" * 81, "group_name": "Training"}, headers=HX)
    assert _is_toast_error(too_long, "Offering name (1–80) and group (1–60) are required.")
    missing = client.get("/setup/offerings/9999/edit", headers=HX)
    assert _is_toast_error(missing, "That item no longer exists")


def test_delete_unused_offering_removes_it_and_used_one_is_deactivated(client: TestClient) -> None:
    products = _offering_id("Products")
    resp = unescape(client.post(f"/setup/offerings/{products}/delete", headers=HX).text)
    assert "Deleted 'Products'." in resp and "<strong>Products" not in resp

    consulting = _offering_id("AI consulting & build services")
    make_enquiry("P8", **triaged("genuine", "Insurer wants an AI assistant built", offering_id=consulting))
    resp = unescape(client.post(f"/setup/offerings/{consulting}/delete", headers=HX).text)
    assert "deactivated — still used by 1 enquiry" in resp
    assert "<strong>AI consulting & build services" not in resp
    with session_scope() as db:
        row = db.get(Offering, consulting)
        assert row is not None and row.active is False


def test_linkedin_card_busy_state_polls_and_signals_header_on_change(client: TestClient) -> None:
    release = threading.Event()
    get_runner().submit("check", lambda: release.wait(10) and None)
    try:
        card = client.get("/setup/linkedin/status", headers=HX)
        assert 'hx-trigger="every 1s"' in card.text and "Checking your LinkedIn session" in card.text
        assert "HX-Trigger" not in card.headers  # no previous status → no change signal

        busy = client.post("/setup/linkedin/connect", headers=HX)
        assert _is_toast_error(busy, "LinkedIn is busy (Re-check in progress) — wait for it to finish.")
        light = client.get("/_shell/status-light", headers=HX).text
        assert "status-light--busy" in light and "Checking…" in light and "every 5s" in light
    finally:
        release.set()
    for _ in range(100):
        if get_runner().busy() is None:
            break
        threading.Event().wait(0.05)
    done = client.get("/setup/linkedin/status", params={"prev": "busy:check"}, headers=HX)
    assert done.headers.get("HX-Trigger") == "session-changed"
    assert "every 1s" not in done.text


def test_needs_attention_card_shows_last_error_and_session_api(client: TestClient) -> None:
    set_session("needs_attention", last_error="Captcha page at /search/results/content/")
    html = unescape(client.get("/setup").text)
    card = html.split('id="linkedin-card"', 1)[1].split("</section>", 1)[0]
    assert "Needs attention" in card and "Captcha page at /search/results/content/" in card
    assert "Connect LinkedIn" in card
    body = client.get("/api/session").json()
    assert body["ok"] is True
    assert body["data"]["status"] == "needs_attention" and body["data"]["last_error"].startswith("Captcha page")

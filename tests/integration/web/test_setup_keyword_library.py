"""Setup keywords card (Phase 1.4): grouped library, yield stats, Add suggested, inline edit."""

from __future__ import annotations

import re
from html import unescape

from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Keyword
from sales0.services.keyword_library import SUGGESTED_KEYWORDS
from tests.integration.web.conftest import HX, make_keyword


def _row(html: str, keyword_id: int) -> str:
    start = html.index(f'<li id="keyword-{keyword_id}"')
    return html[start : html.index("</li>", start)]


def _toast(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html.split('id="toasts"', 1)[-1])) if 'id="toasts"' in html else unescape(html)


def test_add_suggested_seeds_sixty_in_four_groups_and_is_idempotent(client: TestClient) -> None:
    first = client.post("/setup/keywords/suggested", headers=HX)
    text = unescape(first.text)
    assert "Added 60 · 0 already present" in text
    groups = re.findall(r'<details class="keyword-group" data-group="([^"]+)"', text)
    assert len(groups) == 4 and "Your keywords" not in groups
    assert len(re.findall(r"<li id=\"keyword-\d+\"", text)) == 60
    assert all(f"<strong>{k.text}</strong>" in text for k in SUGGESTED_KEYWORDS[:5])
    assert "15 keywords · scanned" in text and text.count("suggested</span>") >= 60

    second = client.post("/setup/keywords/suggested", headers=HX)
    assert "Added 0 · 60 already present" in unescape(second.text)
    with session_scope() as db:
        assert len(db.scalars(select(Keyword)).all()) == 60
    page = unescape(client.get("/setup").text)
    assert 'id="add-suggested-keywords"' in page and len(re.findall(r'data-keyword-id="\d+"', page)) == 60


def test_groups_show_totals_yield_and_no_yield_hint(client: TestClient) -> None:
    good = make_keyword("buyer phrase", group_name="Training", suggested_key="buyer-phrase", scanned_total=200, relevant_total=3)
    dead = make_keyword("dead phrase", group_name="Training", suggested_key="dead-phrase", scanned_total=120, relevant_total=0)
    young = make_keyword("young phrase", scanned_total=40, relevant_total=0)
    html = unescape(client.get("/setup").text)
    heading = re.search(r'data-group="Training".*?</summary>', html, re.S).group(0)  # type: ignore[union-attr]
    assert "2 keywords · scanned 320 · relevant 3" in re.sub(r"<[^>]+>|\s+", lambda m: " " if m.group(0).strip() == "" else "", heading)
    assert 'data-group="Your keywords"' in html
    good_row, dead_row, young_row = _row(html, good), _row(html, dead), _row(html, young)
    assert "keyword__yield" in good_row and "(1.5%)" in re.sub(r"<[^>]+>", "", good_row)
    assert "No yield" not in good_row and "No yield" in dead_row and "No yield" not in young_row
    assert "suggested</span>" in good_row and "suggested</span>" not in young_row
    assert "Scanned 200 · Relevant 3" in re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", good_row))


def test_edit_keyword_changes_text_and_group_and_validates(client: TestClient) -> None:
    kid = make_keyword("old phrase", group_name="Training", suggested_key="old-phrase")
    resp = client.post(f"/setup/keywords/{kid}/edit", data={"text": "  new   phrase ", "group_name": "none"}, headers=HX)
    body = unescape(resp.text)
    assert resp.status_code == 200 and "Saved keyword 'new phrase'." in body
    assert 'data-group="Your keywords"' in body and f'data-keyword-id="{kid}"' in body
    with session_scope() as db:
        row = db.get(Keyword, kid)
        assert (row.text, row.group_name, row.suggested_key) == ("new phrase", None, "old-phrase")

    # group omitted: kept as is
    other = make_keyword("other phrase", group_name="Training")
    client.post(f"/setup/keywords/{other}/edit", data={"text": "other phrase two"}, headers=HX)
    with session_scope() as db:
        assert db.get(Keyword, other).group_name == "Training"

    short = client.post(f"/setup/keywords/{kid}/edit", data={"text": "a"}, headers=HX)
    assert "Keyword must be 2–100 characters." in unescape(short.text) and "toast--error" in short.text
    dup = client.post(f"/setup/keywords/{kid}/edit", data={"text": "OTHER phrase two"}, headers=HX)
    assert "already in your keywords" in unescape(dup.text)
    gone = client.post("/setup/keywords/9999/edit", data={"text": "whatever"}, headers=HX)
    assert "That item no longer exists" in unescape(gone.text)


def test_row_has_edit_form_and_non_htmx_edit_redirects(client: TestClient) -> None:
    kid = make_keyword("form phrase")
    row = unescape(_row(client.get("/setup").text, kid))
    assert f'hx-post="/setup/keywords/{kid}/edit"' in row and "Save" in row and "Cancel" in row
    assert 'name="text"' in row and 'name="group_name"' in row
    resp = client.post(f"/setup/keywords/{kid}/edit", data={"text": "form phrase 2"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    toggled = client.post(f"/setup/keywords/{kid}/toggle", data={"active": "false"}, headers=HX)
    assert toggled.text.lstrip().startswith("<li") and "Paused" in toggled.text

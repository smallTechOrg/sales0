"""Inbox "Skipped by AI" tab (Phase 1.4): list, search, state filter, Rescue / Override / Retry."""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, SeenPost
from tests.integration.web.conftest import HX, make_seen_post


def _rows(html: str) -> list[str]:
    return re.findall(r'<article id="skipped-\d+".*?</article>', html, re.S)


def _count(html: str, key: str) -> int:
    return int(re.search(rf'data-count="{key}">(\d+)<', html).group(1))  # type: ignore[union-attr]


def test_skipped_tab_lists_rows_with_reason_meta_and_buttons(client: TestClient) -> None:
    sid = make_seen_post(times_seen=3, post_url="https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000099/")
    html = unescape(client.get("/inbox?tab=skipped").text)
    assert 'id="inbox-tab-skipped"' in html and "Skipped by AI (" in html and _count(html, "skipped") == 1
    assert 'id="skipped-list"' in html and 'id="skipped-state"' in html and 'id="inbox-search"' in html
    (row,) = _rows(html)
    assert f'id="skipped-{sid}"' in row and 'data-verdict="discard"' in row and 'data-can-rescue="true"' in row
    assert "Agency recruiting freelance trainers" in row and "Training agency" in row
    assert "Agency Person · Talent partner" in row and "ai workshop" in row and "seen 3× · last 2h ago" in row
    assert "View post ↗" in row
    assert "skipped__rescue" in row and "Rescue → enquiry" in row and "skipped__override" in row and "Override: accept" in row
    assert "bulk-bar" not in html  # the Skipped list never has bulk controls
    assert "Select all" not in html


def test_skipped_empty_states_search_and_failed_filter(client: TestClient) -> None:
    assert "Nothing skipped yet — posts the AI judges not relevant are listed here." in client.get("/inbox?tab=skipped").text
    assert "No triage failures." in client.get("/inbox?tab=skipped&state=failed").text
    make_seen_post("Agency recruiting trainers for Delhi cohort", author="Zed Agency", reason="Staffing agency")
    make_seen_post("Thought leadership about the future of AI", author="Amy Writer", reason="Opinion piece, no requirement")
    make_seen_post("Broken one", verdict=None, reason=None, triage_status="failed", triage_error="Rate limited by the API")

    by_author = _rows(client.get("/inbox?tab=skipped&q=ZED").text)  # case-insensitive, author
    assert len(by_author) == 1 and "Zed Agency" in by_author[0]
    by_reason = _rows(client.get("/inbox?tab=skipped&q=opinion").text)
    assert len(by_reason) == 1 and "Amy Writer" in by_reason[0]
    by_snippet = client.get("/inbox?tab=skipped&q=delhi").text
    assert len(_rows(by_snippet)) == 1
    assert "No skipped posts match 'nope'." in client.get("/inbox?tab=skipped&q=nope").text

    failed = unescape(client.get("/inbox?tab=skipped&state=failed").text)
    (row,) = _rows(failed)
    assert "AI triage failed — Rate limited by the API" in row and "skipped__retry" in row and "skipped__rescue" not in row
    assert _count(failed, "skipped") == 2 and "Triage failed (1)" in failed
    htmx = client.get("/inbox?tab=skipped&q=amy", headers={**HX, "HX-Target": "inbox-list"}).text
    assert htmx.lstrip().startswith('<section id="inbox-list"') and "inbox-tabs" not in htmx


def test_skipped_pagination_is_25_per_page(client: TestClient) -> None:
    for i in range(27):
        make_seen_post(f"Skipped post number {i} about nothing relevant at all", author=f"Author {i}")
    first = unescape(client.get("/inbox?tab=skipped").text)
    assert len(_rows(first)) == 25 and "Page 1 of 2 · 27 posts" in first
    assert len(_rows(client.get("/inbox?tab=skipped&page=2").text)) == 2


def test_rescue_moves_post_to_review_and_updates_counts(client: TestClient) -> None:
    sid = make_seen_post()
    resp = client.post(f"/inbox/skipped/{sid}/rescue", headers=HX)
    assert resp.status_code == 200
    body = unescape(resp.text)
    assert "Moved to To review" in body and f'id="skipped-{sid}"' not in body
    assert "Nothing skipped yet" in body and 'hx-swap-oob="true"' in body
    assert _count(body, "skipped") == 0 and _count(body, "review") == 1
    with session_scope() as db:
        row = db.get(SeenPost, sid)
        enquiry = db.get(Enquiry, row.enquiry_id)
        assert row.rescued_at is not None and enquiry.review_status == "pending_review"
        assert enquiry.ai_verdict == "discard"
    again = client.post(f"/inbox/skipped/{sid}/rescue", headers=HX)
    assert "already in your inbox" in unescape(again.text) and "toast--error" in again.text


def test_override_accepts_as_prospect_and_records_override(client: TestClient) -> None:
    sid = make_seen_post()
    resp = client.post(f"/inbox/skipped/{sid}/override", headers=HX)
    assert "Accepted as Prospect · New (overrode AI)" in unescape(resp.text)
    with session_scope() as db:
        enquiry = db.scalar(select(Enquiry))
        assert enquiry.review_status == "accepted" and enquiry.ai_verdict == "discard"
    accepted = unescape(client.get("/inbox?tab=accepted").text)
    assert "Overrode AI" in accepted and "Prospect · New" in accepted


def test_purged_row_disables_buttons_and_server_refuses(client: TestClient) -> None:
    sid = make_seen_post(purged=True)
    (row,) = _rows(unescape(client.get("/inbox?tab=skipped").text))
    assert 'data-can-rescue="false"' in row and "text purged" in row
    assert row.count("disabled") >= 2 and "Post text is no longer stored (kept 14 days)" in row
    resp = client.post(f"/inbox/skipped/{sid}/rescue", headers=HX)
    assert "no longer stored" in unescape(resp.text) and "toast--error" in resp.text
    with session_scope() as db:
        assert db.scalar(select(Enquiry.id)) is None


def test_unknown_skipped_id_is_a_404_toast(client: TestClient) -> None:
    resp = client.post("/inbox/skipped/9999/rescue", headers=HX)
    assert "That item no longer exists" in unescape(resp.text)
    assert client.post("/inbox/skipped/9999/override").status_code in (404, 400)


def test_non_htmx_actions_redirect_to_the_skipped_tab(client: TestClient) -> None:
    sid = make_seen_post()
    resp = client.post(f"/inbox/skipped/{sid}/rescue", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/inbox?tab=skipped")


@pytest.mark.llm
@pytest.mark.timeout(120)
def test_retry_triage_reasks_the_real_ai_and_clears_the_failure(client: TestClient) -> None:
    text = ("Hiring freelance AI trainers for upcoming batches, rates per day, apply now. "
            "We are a training agency sourcing trainers for our corporate clients across India.")
    sid = make_seen_post(text, verdict=None, reason=None, triage_status="failed", triage_error="Rate limited by the API")
    resp = client.post(f"/inbox/skipped/{sid}/retry", headers=HX)
    body = unescape(resp.text)
    assert resp.status_code == 200 and "AI triage finished." in body
    with session_scope() as db:
        row = db.get(SeenPost, sid)
        assert row.triage_status == "done" and row.verdict in ("genuine", "discard")
    assert f'id="skipped-{sid}"' not in body  # no longer a failure

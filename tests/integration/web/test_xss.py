"""XSS regression: LinkedIn post text, author fields and AI output are attacker-controlled.
Every place they render (inbox tabs, search, run detail, runs page) must HTML-escape them."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry
from tests.integration.web.conftest import HX, make_enquiry, make_run, triaged

SCRIPT = "<script>alert(1)</script>"
IMG = '"><img src=x onerror=alert(2)>'
PAYLOAD = f"{SCRIPT} {IMG}"
ESCAPED_SCRIPT = "&lt;script&gt;alert(1)&lt;/script&gt;"
ESCAPED_IMG = "&lt;img src=x onerror=alert(2)&gt;"


def _assert_escaped(html: str, *, min_occurrences: int = 1) -> None:
    assert SCRIPT not in html, "raw <script> payload rendered"
    assert "<img src=x onerror" not in html, "raw <img onerror> payload rendered"
    assert not re.search(r"<[^>]*\sonerror=", html), "an onerror attribute reached the markup"
    assert html.count(ESCAPED_SCRIPT) >= min_occurrences, f"expected ≥{min_occurrences} escaped payloads"
    assert ESCAPED_IMG in html


def _poison(enquiry_id: int, **extra: str) -> None:
    with session_scope() as db:
        e = db.get(Enquiry, enquiry_id)
        assert e is not None
        e.author_name = f"Mallory {PAYLOAD}"
        e.author_headline = f"Headline {PAYLOAD}"
        for key, value in extra.items():
            setattr(e, key, value)


def _seed(run_id: int) -> dict[str, int]:
    fields = {"company": f"Co {PAYLOAD}", "contact_person": f"CP {PAYLOAD}", "city": f"City {PAYLOAD}", "topic": f"T {PAYLOAD}"}
    ids = {
        "review": make_enquiry("P1", text=f"Need a GenAI workshop {PAYLOAD}", run_id=run_id, keyword=f"kw {PAYLOAD}",
                               **triaged("genuine", f"Reason {PAYLOAD}", summary=f"Summary {PAYLOAD}", **fields)),
        "failed": make_enquiry("P6", text=f"Failed triage post {PAYLOAD}", run_id=run_id,
                               triage_status="failed", triage_error=f"AI error {PAYLOAD}"),
        "accepted": make_enquiry("P1", text=f"Accepted post {PAYLOAD}", run_id=run_id,
                                 **triaged("genuine", f"Reason {PAYLOAD}", summary=f"Summary {PAYLOAD}", review_status="accepted")),
        "discarded": make_enquiry("P2", text=f"Discarded post {PAYLOAD}", run_id=run_id,
                                  **triaged("discard", f"Reason {PAYLOAD}", summary=f"Summary {PAYLOAD}", offering_id=None,
                                            review_status="discarded", discard_reason=f"Because {PAYLOAD}")),
    }
    for enquiry_id in ids.values():
        _poison(enquiry_id)
    return ids


@pytest.mark.parametrize("tab", ["review", "accepted", "discarded"])
def test_inbox_tabs_escape_post_author_and_ai_fields(client: TestClient, tab: str) -> None:
    ids = _seed(make_run("succeeded", posts_new=4))
    html = client.get("/inbox", params={"tab": tab}).text
    expected = {"review": [ids["review"], ids["failed"]], "accepted": [ids["accepted"]], "discarded": [ids["discarded"]]}[tab]
    for enquiry_id in expected:
        assert f'id="enquiry-{enquiry_id}"' in html
    # raw_text + author + headline (+ reason/summary when triaged) per card.
    _assert_escaped(html, min_occurrences=3 * len(expected))
    if tab == "review":
        for label in ("Reason", "Summary", "Co", "CP", "City", "AI error", "kw"):
            assert f"{label} {ESCAPED_SCRIPT}" in html, label
    if tab == "discarded":
        assert f"Because {ESCAPED_SCRIPT}" in html

    fragment = client.get("/inbox", params={"tab": tab}, headers=HX).text  # htmx tab swap
    _assert_escaped(fragment)


def test_inbox_search_reflects_the_query_escaped(client: TestClient) -> None:
    _seed(make_run("succeeded", posts_new=4))
    html = client.get("/inbox", params={"q": PAYLOAD}).text
    assert SCRIPT not in html and "<img src=x onerror" not in html
    assert ESCAPED_SCRIPT in html  # the echoed query in the search box / empty-state


def test_decision_response_fragment_is_escaped(client: TestClient) -> None:
    ids = _seed(make_run("succeeded", posts_new=4))
    resp = client.post("/inbox/decide", data={"enquiry_ids": [str(ids["review"])], "decision": "accept", "tab": "review"}, headers=HX)
    assert resp.status_code == 200 and "toast--error" not in resp.text, resp.text
    _assert_escaped(resp.text)  # the re-rendered review list still holds the failed card
    with session_scope() as db:
        assert db.get(Enquiry, ids["review"]).review_status == "accepted"  # type: ignore[union-attr]


def test_run_detail_and_runs_page_escape_keyword_and_error_text(client: TestClient) -> None:
    run_id = make_run(
        "failed", error_message=f"Boom {PAYLOAD}", keywords_planned=1,
        keywords=[(f"kw {PAYLOAD}", "error")],
    )
    _seed(run_id)
    detail = client.get(f"/runs/{run_id}").text
    _assert_escaped(detail, min_occurrences=2)  # keyword cell + outcome line
    assert f"kw {ESCAPED_SCRIPT}" in detail and f"Boom {ESCAPED_SCRIPT}" in detail

    runs_page = client.get("/runs").text
    _assert_escaped(runs_page)  # "Last run failed: Boom …"

    active = make_run("running", keywords_planned=2, current_step=f"Searching {PAYLOAD}")
    _assert_escaped(client.get(f"/runs/{active}/progress", headers=HX).text)
    _assert_escaped(client.get(f"/runs/{active}").text)

"""Pipeline pages (Phase 2 slice S4) through the real app: Due-today panel, board/list, prospect /
lead / client detail pages, timeline notes, follow-ups, stage moves, the prospect -> lead -> client
journey, flags, tracked toggle, JSON endpoints, XSS and the "Draft email" prefill.

Prospects are created through ``services.crm.prospects`` (the contract), never by hand-written SQL,
so these tests also pin that the routes talk to the real services.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.clock import local_today, utcnow
from sales0.config import get_settings
from sales0.db.engine import session_scope
from sales0.db.models import Prospect, SheetSource, TeamMember
from sales0.llm import set_llm_client
from sales0.services import inbox as inbox_service
from sales0.services.crm import followups, prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry, triaged
from tests.support.fake_llm import FakeLLMClient

JSON = {"Accept": "application/json"}
DRAFT = {
    "subject": "AI workshops for Acme Logistics",
    "body": "Hello Priya,\n\nI saw Acme Logistics wants a GenAI workshop. We can help.\n\nBest regards,\nAnn Rao",
}


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: DRAFT))
    with TestClient(create_app()) as c:
        yield c


def _accepted_enquiry(post: str = "P1", **extra) -> int:  # noqa: ANN003
    fields = triaged("genuine", "Company seeking a training partner", summary="Acme Logistics - GenAI workshop for 40 managers")
    fields.update(
        review_status="accepted", company="Acme Logistics", email="priya@acme.example", contact_person="Priya Menon",
        author_profile_url=f"https://www.linkedin.com/in/person-{post.lower()}/",
    )  # fmt: skip
    fields.update(extra)
    return make_enquiry(post, **fields)


def _prospect(post: str = "P1", **extra) -> int:  # noqa: ANN003
    enquiry_id = _accepted_enquiry(post, **extra)
    with session_scope() as db:
        return prospects.ensure_for_enquiry(db, enquiry_id).id


def _today():  # noqa: ANN202
    return local_today(get_settings().tz)


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _post(web: TestClient, path: str, data: dict | None = None, *, hx: bool = False, follow: bool = False):  # noqa: ANN202
    return web.post(path, data=data or {}, headers=HX if hx else {}, follow_redirects=follow)


def _contacted_and_responded(web: TestClient, pid: int) -> None:
    assert _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "contacted"}, hx=True).status_code == 200
    assert _post(web, f"/pipeline/prospects/{pid}/response", hx=True).status_code == 200


# ---------------------------------------------------------------------- empty / render
def test_empty_pipeline_page_has_every_empty_state(web: TestClient) -> None:
    html = web.get("/pipeline").text
    main = _main(html)
    assert 'id="pipeline-tabs"' in main and 'id="due-today"' in main and 'aria-live="polite"' in main
    assert "Nothing due today." in main
    assert "No prospects yet" in main
    for tab in ("Prospects", "Leads", "Clients"):
        assert tab in main
    assert 'id="view-toggle"' in main and 'id="pipeline-search"' in main
    assert "Coming in Phase 2" not in html
    assert "No leads yet" in _main(web.get("/pipeline?tab=leads").text)
    assert "No clients yet" in _main(web.get("/pipeline?tab=clients").text)
    assert 'id="due-badge"' not in html  # nothing due: no badge


def test_accepting_an_enquiry_in_the_inbox_puts_one_prospect_on_the_board(web: TestClient) -> None:
    first = make_enquiry("P1", **triaged("genuine", "Buyer", summary="GenAI workshop"), company="Acme Logistics")
    second = make_enquiry("P8", **triaged("genuine", "Buyer", summary="AI assistant"), company="Acme Logistics")
    resp = web.post("/inbox/decide", data={"enquiry_ids": [str(first), str(second)], "decision": "accept"}, follow_redirects=False)
    assert resp.status_code == 303
    html = web.get("/pipeline").text
    cards = re.findall(r'<article class="pcard" id="prospect-(\d+)" data-stage="new"', html)
    assert 1 <= len(cards) <= 2
    assert 'id="pipeline-board"' in html and 'data-stage="contacted"' in html and "Follow-up 1" in html
    assert 'class="count-pill"' in html


def test_inbox_decide_passes_the_acting_member(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    enquiry_id = make_enquiry("P1", **triaged("genuine", "Buyer", summary="GenAI workshop"))
    seen: dict = {}
    real = inbox_service.decide

    def spy(db, ids, decision, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        seen.update(kwargs)
        return real(db, ids, decision, *args, **kwargs)

    monkeypatch.setattr(inbox_service, "decide", spy)
    assert web.post("/inbox/decide", data={"enquiry_ids": [str(enquiry_id)], "decision": "accept"}, follow_redirects=False).status_code == 303
    with session_scope() as db:
        admin = db.scalars(select(TeamMember).order_by(TeamMember.id)).first()
        assert admin is not None
        assert seen["actor_member_id"] == admin.id


def test_prospect_detail_has_actions_sections_and_a_truthful_convert_button(web: TestClient) -> None:
    pid = _prospect()
    html = web.get(f"/pipeline/prospects/{pid}").text
    main = _main(html)
    for hook in ('id="prospect-header"', 'id="timeline"', 'id="note-form"', 'id="follow-ups"', 'id="btn-draft"', 'id="btn-response"', 'id="btn-convert"'):
        assert hook in main, hook
    assert f'href="/outreach/compose?prospect_id={pid}"' in main
    # Not contacted yet: Convert is disabled and says why.
    assert re.search(r'<button[^>]*id="btn-convert"[^>]*disabled', main)
    assert "Contact it first" in main
    assert "Acme Logistics - GenAI workshop for 40 managers" in unescape(main)  # linked enquiry
    assert "No replies yet." in main and "No emails drafted" in main
    assert "Synced from sheet" not in main
    # The timeline already has the creation entry.
    assert "timeline__body" in main


def test_unknown_ids_are_clean_404s(web: TestClient) -> None:
    for path in ("/pipeline/prospects/99999", "/pipeline/leads/99999", "/pipeline/clients/99999"):
        resp = web.get(path)
        assert resp.status_code == 404 and "That item no longer exists" in resp.text
        assert web.get(path, headers=JSON).json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------- notes, stage, follow-ups
def test_add_note_htmx_returns_oob_timeline_and_toast_plain_redirects(web: TestClient) -> None:
    pid = _prospect()
    resp = _post(web, f"/pipeline/prospects/{pid}/note", {"body": "Called Priya, wants a quote"}, hx=True)
    assert resp.status_code == 200
    assert 'id="timeline-card"' in resp.text and 'hx-swap-oob="outerHTML:#timeline-card"' in resp.text
    assert "Called Priya, wants a quote" in resp.text and "Note added." in resp.text
    plain = _post(web, f"/pipeline/prospects/{pid}/note", {"body": "Second note"})
    assert plain.status_code == 303 and plain.headers["location"] == f"/pipeline/prospects/{pid}"
    page = web.get(f"/pipeline/prospects/{pid}")
    assert "Second note" in page.text and "Called Priya, wants a quote" in page.text
    assert page.text.index("Second note") < page.text.index("Called Priya")  # newest first
    assert "Note added." in page.text and 'id="crm-flash"' in page.text
    assert 'id="crm-flash"' not in web.get(f"/pipeline/prospects/{pid}").text  # the flash is one-shot


def test_empty_note_is_rejected_and_nothing_is_stored(web: TestClient) -> None:
    pid = _prospect()
    htmx = _post(web, f"/pipeline/prospects/{pid}/note", {"body": "   "}, hx=True)
    assert "Write something" in htmx.text and "toast--error" in htmx.text
    plain = _post(web, f"/pipeline/prospects/{pid}/note", {"body": "   "})
    assert plain.status_code == 303
    assert "Write something" in web.get(f"/pipeline/prospects/{pid}").text
    api = _post(web, f"/pipeline/prospects/{pid}/note", {"body": ""})
    assert api.status_code == 303


def test_note_survives_reload_and_json_note_returns_envelope(web: TestClient) -> None:
    pid = _prospect()
    resp = web.post(f"/pipeline/prospects/{pid}/note", data={"body": "Remember the deck"}, headers=JSON)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert "Remember the deck" in web.get(f"/pipeline/prospects/{pid}").text


def test_stage_move_to_contacted_schedules_three_followups_and_updates_fragments(web: TestClient) -> None:
    pid = _prospect()
    resp = _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "contacted"}, hx=True)
    assert resp.status_code == 200
    assert 'hx-swap-oob="outerHTML:#prospect-header"' in resp.text and ">Contacted<" in resp.text
    assert "Moved to Contacted." in resp.text
    with session_scope() as db:
        detail = prospects.get_prospect(db, pid)
        assert detail.stage == "contacted" and len(detail.follow_ups) == 3
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert len(re.findall(r'id="fu-\d+"', page)) == 3 and "Follow-up 1" in page and "Final follow-up" in page
    assert "Stage" in page and re.search(r'<option value="contacted" selected>', page)


def test_invalid_stage_is_an_error_toast_or_flash(web: TestClient) -> None:
    pid = _prospect()
    assert "toast--error" in _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "nonsense"}, hx=True).text
    plain = _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "nonsense"})
    assert plain.status_code == 303
    assert 'flash--error' in web.get(f"/pipeline/prospects/{pid}").text


def test_board_card_stage_move_refreshes_the_board_and_due_panel(web: TestClient) -> None:
    pid = _prospect()
    resp = _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "follow_up_1", "next": "/pipeline?tab=prospects"}, hx=True)
    assert resp.status_code == 200
    assert 'hx-swap-oob="outerHTML:#pipeline-body"' in resp.text and 'hx-swap-oob="outerHTML:#due-today"' in resp.text
    assert re.search(rf'<article class="pcard" id="prospect-{pid}" data-stage="follow_up_1"', resp.text)
    # an open redirect target is ignored
    bad = _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "contacted", "next": "//evil.example"})
    assert bad.status_code == 303 and bad.headers["location"] == f"/pipeline/prospects/{pid}"


def test_due_today_panel_done_snooze_and_open(web: TestClient) -> None:
    pid = _prospect()
    today = _today()
    with session_scope() as db:
        today_fu = followups.add_custom(db, prospect_id=pid, due_on=today, note="Send the agenda")
        past_fu = followups.add_custom(db, prospect_id=pid, due_on=today - timedelta(days=2), note="Chase the quote")
    html = web.get("/pipeline").text
    assert 'id="due-badge"' in html  # nav badge while follow-ups are due
    due = html.split('id="due-today"', 1)[1].split("</section>", 1)[0]
    assert "Send the agenda" in due and "Chase the quote" in due
    assert "Overdue (1)" in due and "Due today (1)" in due and "due-row--overdue" in due
    assert f'href="/pipeline/prospects/{pid}"' in due and "Nothing due today." not in due

    done = _post(web, f"/pipeline/followups/{today_fu.id}/done", {"next": "/pipeline"}, hx=True)
    assert done.status_code == 200 and "Follow-up done." in done.text
    assert "Send the agenda" not in done.text.split('id="due-today"', 1)[1].split("</section>", 1)[0]

    tomorrow = (today + timedelta(days=1)).isoformat()
    snoozed = _post(web, f"/pipeline/followups/{past_fu.id}/snooze", {"due_on": tomorrow}, hx=True)
    assert snoozed.status_code == 200 and "Snoozed until" in snoozed.text
    after = web.get("/pipeline").text.split('id="due-today"', 1)[1].split("</section>", 1)[0]
    assert "Nothing due today." in after

    bad = _post(web, f"/pipeline/followups/{past_fu.id}/snooze", {"due_on": "not-a-date"}, hx=True)
    assert "toast--error" in bad.text
    plain = _post(web, f"/pipeline/followups/{past_fu.id}/done", {"next": "/pipeline"})
    assert plain.status_code == 303 and plain.headers["location"] == "/pipeline"


def test_add_custom_followup_from_the_detail_page(web: TestClient) -> None:
    pid = _prospect()
    due = (_today() + timedelta(days=4)).isoformat()
    resp = _post(web, f"/pipeline/prospects/{pid}/followup", {"due_on": due, "note": "Ask about budget"}, hx=True)
    assert resp.status_code == 200 and 'hx-swap-oob="outerHTML:#follow-ups"' in resp.text and "Ask about budget" in resp.text
    assert "toast--error" in _post(web, f"/pipeline/prospects/{pid}/followup", {"due_on": ""}, hx=True).text
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert "Ask about budget" in page


# ---------------------------------------------------------------------- lead journey
def test_convert_is_refused_until_contacted_and_responded_then_creates_the_lead(web: TestClient) -> None:
    pid = _prospect()
    early = _post(web, f"/pipeline/prospects/{pid}/convert", hx=True)
    assert "toast--error" in early.text and "not been contacted" in early.text
    plain = _post(web, f"/pipeline/prospects/{pid}/convert")
    assert plain.status_code == 303
    assert "has not been contacted" in web.get(f"/pipeline/prospects/{pid}").text

    assert _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "contacted"}, hx=True).status_code == 200
    assert "No response is recorded yet" in _post(web, f"/pipeline/prospects/{pid}/convert", hx=True).text
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert re.search(r'<button[^>]*id="btn-convert"[^>]*disabled', page) and "Record a response first" in page

    assert _post(web, f"/pipeline/prospects/{pid}/response", {"note": "Replied on the phone"}, hx=True).status_code == 200
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert "Response recorded" in page and not re.search(r'<button[^>]*id="btn-convert"[^>]*disabled', page)

    meeting = (utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT10:00")
    resp = _post(web, f"/pipeline/prospects/{pid}/convert", {"meeting_at": meeting}, hx=True)
    assert resp.status_code == 204
    lead_url = resp.headers["hx-redirect"]
    assert re.fullmatch(r"/pipeline/leads/\d+", lead_url)
    lead_page = web.get(lead_url)
    assert lead_page.status_code == 200 and "Meeting scheduled" in lead_page.text and 'id="lead-stepper"' in lead_page.text
    assert "Converted to a lead." in lead_page.text  # flash
    assert 'aria-current="step">Meeting scheduled' in lead_page.text
    # The prospect now links to the lead and shows Converted.
    assert f'href="{lead_url}"' in web.get(f"/pipeline/prospects/{pid}").text
    assert re.search(r'id="lead-\d+" data-stage="meeting_scheduled"', web.get("/pipeline?tab=leads").text)
    again = _post(web, f"/pipeline/prospects/{pid}/convert", hx=True)
    assert "toast--error" in again.text


def test_lead_stages_proposal_won_creates_a_client_and_timeline_lists_every_step(web: TestClient) -> None:
    pid = _prospect()
    _contacted_and_responded(web, pid)
    lead_url = _post(web, f"/pipeline/prospects/{pid}/convert", hx=True).headers["hx-redirect"]
    lead_id = int(lead_url.rsplit("/", 1)[1])

    proposal = _post(
        web, f"/pipeline/leads/{lead_id}/stage",
        {"stage": "proposal_sent", "proposal_sent_on": _today().isoformat(), "value_estimate": "250000"}, hx=True,
    )  # fmt: skip
    assert proposal.status_code == 200 and "Proposal sent" in proposal.text and "250000" in proposal.text
    note = _post(web, f"/pipeline/leads/{lead_id}/note", {"body": "Proposal v2 shared"}, hx=True)
    assert "Proposal v2 shared" in note.text
    won = _post(web, f"/pipeline/leads/{lead_id}/stage", {"stage": "won", "outcome_note": "PO received"}, hx=True)
    assert won.status_code == 200 and "Client created" in won.text
    client_href = re.search(r'Client created &rarr; <a href="(/pipeline/clients/\d+)"', won.text)
    assert client_href, won.text[:1500]

    lead_page = web.get(lead_url).text
    for needle in ("Proposal sent", "Proposal v2 shared", "PO received"):
        assert needle in lead_page, needle
    client_page = web.get(client_href.group(1))
    assert client_page.status_code == 200 and 'id="client-header"' in client_page.text
    clients = _main(web.get("/pipeline?tab=clients").text)
    assert 'id="client-list"' in clients and client_href.group(1) in clients
    # the prospect timeline carries the whole story
    timeline = web.get(f"/pipeline/prospects/{pid}").text
    assert "Replied" in timeline or "Response" in timeline


def test_lost_lead_stops_tracking_and_board_columns_show_it(web: TestClient) -> None:
    pid = _prospect()
    _contacted_and_responded(web, pid)
    lead_id = int(_post(web, f"/pipeline/prospects/{pid}/convert", hx=True).headers["hx-redirect"].rsplit("/", 1)[1])
    resp = _post(web, f"/pipeline/leads/{lead_id}/stage", {"stage": "lost", "outcome_note": "Budget cut"}, hx=True)
    assert resp.status_code == 200 and "Lost" in resp.text
    with session_scope() as db:
        assert prospects.get_prospect(db, pid).tracked is False
    board = web.get("/pipeline?tab=leads").text
    assert f'id="lead-{lead_id}" data-stage="lost"' in board
    assert "toast--error" in _post(web, f"/pipeline/leads/{lead_id}/stage", {"stage": "bogus"}, hx=True).text


def test_board_lead_card_stage_move_with_next(web: TestClient) -> None:
    pid = _prospect()
    _contacted_and_responded(web, pid)
    lead_id = int(_post(web, f"/pipeline/prospects/{pid}/convert", hx=True).headers["hx-redirect"].rsplit("/", 1)[1])
    resp = _post(web, f"/pipeline/leads/{lead_id}/stage", {"stage": "proposal_sent", "next": "/pipeline?tab=leads"}, hx=True)
    assert resp.status_code == 200
    assert re.search(rf'<article class="pcard" id="lead-{lead_id}" data-stage="proposal_sent"', resp.text)


# ---------------------------------------------------------------------- close / reopen / tracked / flag
def test_close_reopen_tracked_and_do_not_follow_up(web: TestClient) -> None:
    pid = _prospect()
    assert _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "contacted"}, hx=True).status_code == 200

    flag = _post(web, f"/pipeline/prospects/{pid}/flag", {"kind": "no_follow_up", "on": "true"}, hx=True)
    assert "flag--no_follow_up" in flag.text and "reminders cancelled" in flag.text
    page = web.get(f"/pipeline/prospects/{pid}").text
    assert "Allow follow-ups again" in page and "No open follow-ups." in page
    assert "flag--no_follow_up" in web.get("/pipeline").text
    cleared = _post(web, f"/pipeline/prospects/{pid}/flag", {"kind": "no_follow_up", "on": "false"}, hx=True)
    assert "flag--no_follow_up" not in cleared.text.split('id="prospect-header"', 1)[1].split("</section>", 1)[0]
    assert "toast--error" in _post(web, f"/pipeline/prospects/{pid}/flag", {"kind": "weird"}, hx=True).text

    tracked = _post(web, f"/pipeline/prospects/{pid}/tracked", {"tracked": "false"}, hx=True)
    assert "Stopped tracking" in tracked.text and "Not tracked" in tracked.text
    assert "Now tracking" in _post(web, f"/pipeline/prospects/{pid}/tracked", {"tracked": "true"}, hx=True).text

    closed = _post(web, f"/pipeline/prospects/{pid}/close", {"reason": "not_interested"}, hx=True)
    assert "Prospect closed." in closed.text and 'id="status-badge"' in closed.text and 'id="btn-reopen"' in closed.text
    assert f'id="prospect-{pid}"' not in web.get("/pipeline").text  # open board hides it
    listing = web.get("/pipeline?tab=prospects&status=closed").text
    assert f'id="prospect-{pid}"' in listing and 'id="pipeline-list"' in listing
    assert "Prospect reopened." in _post(web, f"/pipeline/prospects/{pid}/reopen", hx=True).text
    assert f'id="prospect-{pid}"' in web.get("/pipeline").text
    # a closed prospect refuses stage moves
    _post(web, f"/pipeline/prospects/{pid}/close", {"reason": "lost"}, hx=True)
    assert "toast--error" in _post(web, f"/pipeline/prospects/{pid}/stage", {"stage": "follow_up_1"}, hx=True).text


def test_edit_details_saves_name_and_notes(web: TestClient) -> None:
    pid = _prospect()
    resp = _post(web, f"/pipeline/prospects/{pid}/edit", {"name": "Priya Menon (Acme)", "company": "Acme Logistics", "notes": "Prefers WhatsApp"}, hx=True)
    assert resp.status_code == 200 and "Priya Menon (Acme)" in resp.text and "Details saved." in resp.text
    assert "Prefers WhatsApp" in web.get(f"/pipeline/prospects/{pid}").text
    assert "needs a name" in _post(web, f"/pipeline/prospects/{pid}/edit", {"name": " "}, hx=True).text


# ---------------------------------------------------------------------- create / clients
def test_new_prospect_form_creates_and_reuses(web: TestClient) -> None:
    resp = _post(web, "/pipeline/prospects", {"name": "Ravi Kumar", "company": "Zen Corp", "email": "ravi@zen.example", "notes": "Met at expo"})
    assert resp.status_code == 303 and re.fullmatch(r"/pipeline/prospects/\d+", resp.headers["location"])
    page = web.get(resp.headers["location"]).text
    assert "Ravi Kumar" in page and "Zen Corp" in page and "Met at expo" in page and "Added Ravi Kumar." in page
    again = _post(web, "/pipeline/prospects", {"name": "Ravi K", "email": "ravi@zen.example"})
    assert again.headers["location"] == resp.headers["location"]  # adopted, not duplicated
    blank = _post(web, "/pipeline/prospects", {"name": " "})
    assert blank.status_code == 303 and blank.headers["location"] == "/pipeline?tab=prospects"
    assert "needs a name" in web.get("/pipeline?tab=prospects").text
    created_json = web.post("/pipeline/prospects", data={"name": "Json Person"}, headers=JSON)
    assert created_json.status_code == 201 and created_json.json()["data"]["name"] == "Json Person"


def test_legacy_client_create_update_note(web: TestClient) -> None:
    resp = _post(web, "/pipeline/clients", {"name": "Old Customer Ltd", "company": "Old Customer Ltd", "started_on": "2024-03-01", "notes": "Since 2024"})
    assert resp.status_code == 303 and re.fullmatch(r"/pipeline/clients/\d+", resp.headers["location"])
    url = resp.headers["location"]
    page = web.get(url).text
    assert "Old Customer Ltd" in page and "Client since 01 Mar 2024" in page and "Since 2024" in page
    assert "Old Customer Ltd" in web.get("/pipeline?tab=clients").text
    upd = _post(web, url, {"name": "Old Customer Pvt Ltd", "notes": "Renewed", "status": "legacy"}, hx=True)
    assert upd.status_code == 200 and "Old Customer Pvt Ltd" in upd.text and "Client saved." in upd.text
    note = _post(web, f"{url}/note", {"body": "Renewal call booked"}, hx=True)
    assert "Renewal call booked" in note.text
    assert "Renewal call booked" in web.get(url).text
    assert "needs a name" in _post(web, "/pipeline/clients", {"name": ""}, hx=True).text
    assert web.get("/pipeline?tab=clients&q=zzzz").text.count("No clients match 'zzzz'") == 1


# ---------------------------------------------------------------------- filters, list, pagination
def test_search_and_filters_return_a_fragment_for_htmx_and_a_page_otherwise(web: TestClient) -> None:
    a = _prospect("P1", company="Acme Logistics")
    b = _prospect("P8", company="Globex Insurance", email="x@globex.example", contact_person="Gina Globex")
    assert a != b
    frag = web.get("/pipeline?tab=prospects&q=Globex", headers=HX)
    assert frag.status_code == 200 and "<html" not in frag.text and 'id="pipeline-body"' in frag.text
    assert f'id="prospect-{b}"' in frag.text and f'id="prospect-{a}"' not in frag.text
    empty = web.get("/pipeline?tab=prospects&q=nomatchatall", headers=HX).text
    assert "No prospects" in empty or "Nothing matches" in empty or "Nothing here." in empty
    listing = web.get("/pipeline?tab=prospects&view=list").text
    assert 'id="pipeline-list"' in listing and "<th scope=\"col\">Next follow-up</th>" in listing
    forced = web.get("/pipeline?tab=prospects&stage=contacted").text
    assert 'id="board-note"' in forced and "Nothing matches these filters." in forced
    assert "Coming" not in forced
    bogus = web.get("/pipeline?tab=nonsense&view=huh&status=weird&page=-4")
    assert bogus.status_code == 200 and 'id="pipeline-tabs"' in bogus.text


def test_list_pagination(web: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_CRM_PAGE_SIZE", "5")  # the setting's minimum
    from sales0.config import reset_settings

    reset_settings()
    for n in range(12):
        with session_scope() as db:
            prospects.create_manual(db, name=f"Person {n}", email=f"p{n}@x.example")
    page1 = web.get("/pipeline?tab=prospects&view=list").text
    assert "Page 1 of 3" in page1 and "Next &rarr;" in page1 and "Prev" not in page1.split('aria-label="Pages"')[1]
    page3 = web.get("/pipeline?tab=prospects&view=list&page=3").text
    assert "Page 3 of 3" in page3 and "Prev" in page3
    assert len(re.findall(r'<tr id="prospect-\d+"', page1)) == 5
    assert len(re.findall(r'<tr id="prospect-\d+"', page3)) == 2


# ---------------------------------------------------------------------- sheet-fed rows
def test_sheet_fed_prospect_shows_the_read_only_sheet_panel_and_removed_banner(web: TestClient) -> None:
    pid = _prospect()
    with session_scope() as db:
        row = db.get(Prospect, pid)
        assert row is not None
        row.source = "sheet"
        row.sheet_status = "Replied"
        row.sheet_removed_at = utcnow() - timedelta(days=1)
        db.add(
            SheetSource(
                entity="prospect", key=f"k{pid}", target_type="prospect", target_id=pid, sheet_id="sheet-a", gid="0",
                tab_name="Outreach", row_number=14, row_hash="h", fields={}, first_seen_at=utcnow(), last_seen_at=utcnow(),
            )
        )  # fmt: skip
    html = web.get(f"/pipeline/prospects/{pid}").text
    assert 'id="sheet-panel"' in html and "Read-only" in html
    assert "Synced from sheet" in html and "edits are overwritten" in html
    assert "Outreach" in html and ">14<" in html and "Replied" in html
    assert 'id="sheet-removed"' in html and "Removed from the Google Sheet on" in html
    board = web.get("/pipeline").text
    assert f'id="prospect-{pid}"' not in board or "Removed from sheet" in board
    shown = web.get("/pipeline?tab=prospects&show_removed=1").text
    assert f'id="prospect-{pid}"' in shown and "Removed from sheet" in shown
    assert "chip--sheet" in shown


# ---------------------------------------------------------------------- JSON
def test_json_endpoints(web: TestClient) -> None:
    pid = _prospect()
    summary = web.get("/api/pipeline/summary").json()
    assert summary["ok"] is True and summary["data"]["prospects_by_stage"]["new"] == 1
    detail = web.get(f"/api/prospects/{pid}").json()["data"]
    assert detail["id"] == pid and detail["stage"] == "new" and detail["enquiries"] and detail["timeline"]
    missing = web.get("/api/prospects/99999")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    stage = web.post(f"/pipeline/prospects/{pid}/stage", data={"stage": "contacted"}, headers=JSON)
    assert stage.status_code == 200 and stage.json()["data"]["stage"] == "contacted"
    err = web.post(f"/pipeline/prospects/{pid}/stage", data={"stage": "zzz"}, headers=JSON)
    assert err.status_code >= 400 and err.json()["ok"] is False


# ---------------------------------------------------------------------- XSS
def test_attacker_controlled_text_is_escaped_everywhere(web: TestClient) -> None:
    script = "<script>alert(1)</script>"
    with session_scope() as db:
        view = prospects.create_manual(db, name=f"Mallory {script}", company=f"Evil {script}", notes=f"notes {script}")
    pid = view.id
    assert _post(web, f"/pipeline/prospects/{pid}/note", {"body": f"hello {script}"}, hx=True).status_code == 200
    due = (_today()).isoformat()
    _post(web, f"/pipeline/prospects/{pid}/followup", {"due_on": due, "note": f"call {script}"}, hx=True)
    for path in (f"/pipeline/prospects/{pid}", "/pipeline", "/pipeline?view=list"):
        html = web.get(path).text
        assert script not in html, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, path
    frag = _post(web, f"/pipeline/prospects/{pid}/note", {"body": f"again {script}"}, hx=True).text
    assert script not in frag


# ---------------------------------------------------------------------- Draft email prefill
def test_draft_email_button_prefills_compose_and_forwards_the_prospect(web: TestClient) -> None:
    pid = _prospect()
    with session_scope() as db:
        detail = prospects.get_prospect(db, pid)
        company_id = detail.company_id
    html = web.get(f"/outreach/compose?prospect_id={pid}").text
    assert 'id="compose-form"' in html and f'name="prospect_id" value="{pid}"' in html
    assert 'value="priya@acme.example"' in html and "Acme Logistics" in html
    assert "Acme Logistics - GenAI workshop for 40 managers" in unescape(html)
    assert f'prospect #{pid}' in html
    assert company_id is None or f'name="company_id" value="{company_id}"' in html

    draft = web.post(
        "/outreach/draft",
        data={"to": "priya@acme.example", "company": "Acme Logistics", "contact_name": "Priya Menon", "requirement": "GenAI workshop",
              "prospect_id": str(pid), "company_id": str(company_id or "")},
        follow_redirects=False,
    )  # fmt: skip
    assert draft.status_code == 303 and "/outreach/messages/" in draft.headers["location"]
    from sales0.db.models import OutreachMessage

    with session_scope() as db:
        msg = db.scalars(select(OutreachMessage).order_by(OutreachMessage.id.desc())).first()
        assert msg is not None and msg.prospect_id == pid
    assert web.get("/outreach/compose?prospect_id=99999").status_code == 404
    assert web.get("/outreach/compose").status_code == 200  # the old entry points keep working
    assert 'name="prospect_id" value=""' in web.get("/outreach/compose").text


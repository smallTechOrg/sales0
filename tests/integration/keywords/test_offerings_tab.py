"""Setup > Offerings & keywords: each offering as a card (its enquiry results, its keywords with tiers, its own triage
rules), editable in place; criteria can belong to an offering and reach the AI as "only for posts about <offering>"."""

from __future__ import annotations

from collections.abc import Iterator
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Keyword, Offering, TriageCriteriaSet
from sales0.services import criteria as criteria_service
from sales0.services import keywords as kw
from sales0.services import offerings as offerings_service
from sales0.services.views import OfferingInput
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _offering(name: str, group: str = "Training", description: str = "Hands-on GenAI workshops") -> int:
    with session_scope() as db:
        return offerings_service.create_offering(db, OfferingInput(name=name, group_name=group, description=description)).id


def test_offering_rules_reach_the_prompt_scoped_and_wait_while_the_offering_is_off(db_url: str) -> None:
    oid = _offering("Workshops")
    with session_scope() as db:
        criteria_service.create_rule(db, "exclude", "A college asking for free student workshops.", offering_id=oid)
        criteria_service.create_rule(db, "exclude", "Spam about crypto trading signals.")
        s = criteria_service.active_set(db)
        assert '(Only for posts about the offering "Workshops") A college asking for free student workshops.' in s.exclude
        assert "Spam about crypto trading signals." in s.exclude
        snap = db.scalar(select(TriageCriteriaSet).order_by(TriageCriteriaSet.version.desc()))
        assert {"kind": "exclude", "text": "A college asking for free student workshops.", "offering": "Workshops"} in snap.rules
        # the same text may exist once globally and once per offering
        criteria_service.create_rule(db, "exclude", "A college asking for free student workshops.")
        offerings_service.set_offering_active(db, oid, False)
        off = criteria_service.active_set(db)
        assert not any("Workshops" in r for r in off.exclude) and "A college asking for free student workshops." in off.exclude
        views = criteria_service.list_rules(db, offering_id=oid)
        assert len(views) == 1 and views[0].offering_name == "Workshops" and views[0].offering_active is False
        moved = criteria_service.update_rule(db, views[0].id, offering_id=None, text="A college asking for free guest lectures.")
        assert moved.offering_id is None


def test_offering_cards_show_results_keywords_and_rules_and_edit_in_place(web: TestClient) -> None:
    oid = _offering("Workshops")
    empty = _offering("Agentic builds", group="Build", description="")
    with session_scope() as db:
        v = kw.add_keyword(db, "ai workshop", offering_id=oid)
        row = db.get(Keyword, v.id)
        row.scanned_total, row.relevant_total = 100, 12
        criteria_service.create_rule(db, "include", "A company wants an on-site GenAI workshop for staff.", offering_id=oid)
    make_enquiry("P1", keyword="ai workshop", review_status="accepted", ai_verdict="genuine", offering_id=oid, deal_status="won")

    html = unescape(web.get("/keywords").text)
    assert 'id="setup-tabs"' in html and "Offerings & keywords" in html and 'aria-current="page">Offerings & keywords' in html
    card = html[html.index(f'id="offering-card-{oid}"'):]
    assert "Enquiries matched 1 · accepted 1 · contacted 1 · leads 0 · won 1" in card.replace('<span class="mono">', "").replace("</span>", "")
    assert "Criteria for this offering (1 active)" in card and "on-site GenAI workshop" in card
    assert f'id="offering-card-{empty}"' in html and "No description - the AI matches" in html  # offerings without keywords still listed
    assert html.index(f'id="offering-card-{oid}"') < html.index("ai workshop", html.index(f'id="offering-card-{oid}"'))

    r = web.post(f"/keywords/offerings/{oid}/criteria", headers=HX, data={"kind": "exclude", "text": "Students asking for free internships."})
    assert "Added exclude rule for Workshops." in unescape(r.text) and "Criteria for this offering (2 active)" in unescape(r.text)
    r = web.post(f"/keywords/offerings/{oid}", headers=HX, data={"name": "GenAI Workshops", "group_name": "Training", "description": "Two-day hands-on"})
    assert "Saved offering 'GenAI Workshops'." in unescape(r.text)
    r = web.post(f"/keywords/offerings/{oid}/toggle", headers=HX, data={"active": "false"})
    assert "is off - its keywords and rules are skipped" in unescape(r.text)
    r = web.post("/keywords/offerings", headers=HX, data={"name": "Consulting", "group_name": "Advisory", "description": "AI strategy"})
    assert "Added offering 'Consulting' to Advisory." in unescape(r.text)
    r = web.post(f"/keywords/offerings/{empty}/delete", headers=HX, data={})
    assert "Offering deleted." in unescape(r.text)
    r = web.post("/keywords/library", headers=HX, data={})
    assert "starter keywords" in unescape(r.text)
    with session_scope() as db:
        assert db.get(Offering, oid).name == "GenAI Workshops" and db.get(Offering, oid).active is False
        assert db.get(Offering, empty) is None


def test_setup_has_tabs_and_nav_keeps_setup_highlighted(web: TestClient) -> None:
    setup = unescape(web.get("/setup").text)
    assert 'id="setup-tabs"' in setup and 'id="keyword-input"' not in setup and 'id="offering-list"' not in setup
    crit = unescape(web.get("/criteria").text)
    assert 'id="setup-tabs"' in crit and 'id="new-offering"' in crit and "Every post" in crit
    nav = crit[crit.index('aria-label="Primary"'):crit.index("</nav>", crit.index('aria-label="Primary"'))]
    assert 'href="/setup" aria-current="page"' in nav and 'href="/criteria"' not in nav and 'href="/keywords"' not in nav


def test_criteria_page_filters_and_assigns_rules_to_offerings(web: TestClient) -> None:
    oid = _offering("Workshops")
    r = web.post("/criteria", data={"kind": "include", "text": "A company asking for an AI workshop.", "offering_id": str(oid)})
    assert r.status_code == 200 and "Added include rule for Workshops." in unescape(r.text)
    with session_scope() as db:
        rule = criteria_service.list_rules(db, offering_id=oid)[0]
    only = unescape(web.get(f"/criteria?offering={oid}").text)
    assert "A company asking for an AI workshop." in only and only.count('data-kind="include"') == 1
    assert f'data-offering="{oid}"' in only
    web.post(f"/criteria/{rule.id}", data={"text": rule.text, "kind": "include", "offering_id": "none"})
    with session_scope() as db:
        assert criteria_service.list_rules(db, offering_id=oid) == []


def test_discard_reasons_are_split_by_offering(web: TestClient) -> None:
    from sales0.clock import utcnow
    from sales0.db.models import TriageDecision
    from tests.integration.web.conftest import make_seen_post

    ws, cons = _offering("Workshops"), _offering("Consulting", group="Advisory")
    with session_scope() as db:
        kw.add_keyword(db, "ai workshop", offering_id=ws)
        kw.add_keyword(db, "ai consulting", offering_id=cons)

    def discard(post: str, keyword: str, reason: str, **fields) -> int:  # type: ignore[no-untyped-def]
        eid = make_enquiry(post, keyword=keyword, review_status="discarded", ai_verdict="genuine", **fields)
        with session_scope() as db:
            db.add(TriageDecision(enquiry_id=eid, decision="discard", ai_verdict="genuine", is_override=True, reason=reason,
                                  decided_at=utcnow()))
        return eid

    discard("P1", "ai workshop", "Student asking for free help")           # via its keyword -> Workshops
    discard("P2", "something else", "Student asking for free help", offering_id=ws)  # matched by the AI -> Workshops
    discard("P3", "ai consulting", "Vendor pitch")                         # -> Consulting
    make_seen_post(keyword_text="AI Workshop")                             # AI skip found by a Workshops keyword
    with session_scope() as db:
        by = criteria_service.discards_by_offering(db)
        assert (by[ws].team, by[ws].ai, by[ws].top_reasons) == (2, 1, [("Student asking for free help", 2)])
        assert (by[cons].team, by[cons].ai) == (1, 0)
        only = criteria_service.discard_breakdown(db, offering_id=ws)
        assert [(g.label, g.count_all) for g in only.team] == [("Student asking for free help", 2)] and only.ai[0].count_all == 1
        assert {g.label for g in criteria_service.discard_breakdown(db).team} == {"Student asking for free help", "Vendor pitch"}
    card = unescape(web.get("/keywords").text)
    card = card[card.index(f'id="offering-discards-{ws}"'):][:600]
    assert '"Student asking for free help" (2)' in card and f'/criteria?offering={ws}#discard-reasons' in card
    crit = unescape(web.get(f"/criteria?offering={cons}").text)
    reasons = crit[crit.index('id="discard-reasons"'):]
    assert "Vendor pitch" in reasons and "Student asking for free help" not in reasons and "Only posts that belong" in reasons

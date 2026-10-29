"""The Pipeline page tabs: Enquiries (default) | Prospects | Leads | Clients, their counts, columns and lanes, the old
URLs (``?view=companies``, ``?stage=``, ``/leads`` ...) and the one enquiry status control on the Enquiries board."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from sales0.clock import utcnow
from sales0.db.engine import session_scope
from sales0.db.models import Client, Enquiry, Lead, Prospect
from sales0.llm import set_llm_client
from sales0.services.crm import deals as deals_service
from sales0.services.crm import prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry, triaged
from tests.support.fake_llm import FakeLLMClient


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: {"subject": "s", "body": "Hello"}))
    with TestClient(create_app()) as c:
        yield c


def _company(name: str, post: str) -> tuple[int, int, int]:
    """An accepted enquiry + its prospect + company. Returns (enquiry id, prospect id, company id)."""
    fields = triaged("genuine", "Buyer", summary=f"{name} - GenAI workshop")
    fields.update(review_status="accepted", company=name, email=f"x@{post.lower()}.example", contact_person="Priya",
                  author_profile_url=f"https://www.linkedin.com/in/person-{post.lower()}/")
    eid = make_enquiry("P1", text=f"We need AI training for {name}, ref {post}.", **fields)
    with session_scope() as db:
        p = prospects.ensure_for_enquiry(db, eid)
        return eid, p.id, p.company_id  # type: ignore[return-value]


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _count(html: str, key: str) -> int:
    m = re.search(rf'id="tab-{key}"[^>]*>[^<]*<span class="count-pill">(\d+)</span>', html)
    assert m, key
    return int(m.group(1))


def _col(html: str, key: str) -> str:
    """The inner HTML of one board column (``data-stage`` / ``data-status``)."""
    m = re.search(rf'<section class="board__col[^"]*"[^>]*data-(?:stage|status)="{key}".*?</section>', html, re.S)
    assert m, key
    return m.group(0)


def test_default_tab_is_enquiries_with_four_tabs_and_counts(web: TestClient) -> None:
    _company("Acme Logistics", "P1")
    _, _, lead_cid = _company("Globex Insurance", "P2")
    assert web.post(f"/companies/{lead_cid}/mark-lead", follow_redirects=False).status_code == 303
    _company("Initech", "P3")
    html = web.get("/pipeline").text
    main = _main(html)
    assert 'id="enquiry-board"' in main and 'id="pipeline-board"' not in main
    assert re.search(r'id="tab-enquiries"[^>]*aria-current="page"', main)
    labels = re.findall(r'id="tab-(\w+)"', main)
    assert labels == ["enquiries", "prospects", "leads", "clients"]
    assert (_count(main, "enquiries"), _count(main, "prospects"), _count(main, "leads"), _count(main, "clients")) == (3, 2, 1, 0)
    assert 'id="pipeline-help"' in main and "accepted enquiry" in main
    assert "Inbox" in main  # to-review items are not here, the help says where they stay


def test_each_tab_has_its_own_help_text_and_unknown_tab_falls_back(web: TestClient) -> None:
    helps = {}
    for tab in ("enquiries", "prospects", "leads", "clients"):
        page = web.get(f"/pipeline?tab={tab}")
        assert page.status_code == 200
        helps[tab] = re.search(r'<p id="pipeline-help">(.*?)</p>', page.text, re.S).group(1)  # type: ignore[union-attr]
        assert helps[tab].startswith("What is this?")
    assert len(set(helps.values())) == 4
    fallback = web.get("/pipeline?tab=nonsense").text
    assert re.search(r'id="tab-enquiries"[^>]*aria-current="page"', fallback)


def test_old_urls_map_to_the_matching_tab(web: TestClient) -> None:
    for url, tab in (("/pipeline?view=companies", "prospects"), ("/pipeline?view=enquiries", "enquiries"),
                     ("/pipeline?stage=prospect", "prospects"), ("/pipeline?stage=lead", "leads"), ("/pipeline?stage=client", "clients"),
                     ("/pipeline?stage=closed", "prospects"), ("/pipeline?tab=leads", "leads"), ("/pipeline?tab=prospects&view=list", "prospects")):
        r = web.get(url)
        assert r.status_code == 200, url
        assert re.search(rf'id="tab-{tab}"[^>]*aria-current="page"', r.text), url
    for path, target in (("/leads", "/pipeline?tab=leads"), ("/clients", "/pipeline?tab=clients"), ("/prospects", "/pipeline?tab=prospects")):
        assert web.get(path, follow_redirects=False).headers["location"] == target
    assert "Companies</a>" not in _main(web.get("/pipeline?view=companies").text).split('id="pipeline-tabs"', 1)[1].split("</nav>", 1)[0]


def test_prospects_tab_columns_hold_only_prospects(web: TestClient) -> None:
    _, _, cid_a = _company("Acme Logistics", "P1")
    _, pid_b, cid_b = _company("Beta Skills", "P2")
    _, _, cid_lead = _company("Globex Insurance", "P3")
    _, _, cid_client = _company("Old Client Ltd", "P4")
    _, pid_closed, cid_closed = _company("Closed Co", "P5")
    assert web.post(f"/pipeline/prospects/{pid_b}/stage", data={"stage": "follow_up_1"}, headers=HX).status_code == 200
    web.post(f"/companies/{cid_lead}/mark-lead")
    web.post(f"/companies/{cid_client}/mark-client")
    web.post(f"/pipeline/prospects/{pid_closed}/close", data={"reason": "not_interested"}, headers=HX)
    html = web.get("/pipeline?tab=prospects").text
    keys = re.findall(r'<section class="board__col[^"]*"[^>]*data-stage="(\w+)"', html)
    assert keys == ["new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up", "became_lead"]
    assert 'data-stage="client"' not in html.replace(_col(html, "became_lead"), "")
    assert f'id="company-{cid_a}"' in _col(html, "new") and f'id="company-{cid_b}"' in _col(html, "follow_up_1")
    for key in ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up"):
        assert f'id="company-{cid_lead}"' not in _col(html, key) and f'id="company-{cid_client}"' not in _col(html, key)
    assert f'id="company-{cid_client}"' not in html  # a client is nowhere on the Prospects tab
    # the Became-lead lane (recent) holds the lead and links to the Leads tab
    lane = _col(html, "became_lead")
    assert f'id="company-{cid_lead}"' in lane and 'href="/pipeline?tab=leads&amp;q=Globex+Insurance"' in lane.replace("&q=", "&amp;q=")
    # off-ramp prospects stay in the collapsed Closed lane
    closed = html.split('id="closed-lane"', 1)[1]
    assert f'id="company-{cid_closed}"' in closed and f'id="company-{cid_closed}"' not in html.split('id="closed-lane"', 1)[0]


def test_became_lead_lane_is_recent_only(web: TestClient) -> None:
    _, _, cid = _company("Globex Insurance", "P1")
    web.post(f"/companies/{cid}/mark-lead")
    assert f'id="company-{cid}"' in _col(web.get("/pipeline?tab=prospects").text, "became_lead")
    with session_scope() as db:
        db.execute(update(Lead).values(created_at=utcnow() - timedelta(days=45)))
    html = web.get("/pipeline?tab=prospects").text
    assert f'id="company-{cid}"' not in html  # older than 30 days: not on the Prospects tab at all
    assert f'id="company-{cid}"' in web.get("/pipeline?tab=leads").text  # still a lead


def test_leads_tab_columns_won_lane_and_clients_list(web: TestClient) -> None:
    _, _, cid_m = _company("Meet Co", "P1")
    _, _, cid_p = _company("Proposal Co", "P2")
    eid_w, _, cid_w = _company("Won Co", "P3")
    _, _, cid_prospect = _company("Still Prospect", "P4")
    web.post(f"/companies/{cid_m}/mark-lead")
    web.post(f"/companies/{cid_p}/mark-lead")
    web.post(f"/companies/{cid_p}/status", data={"sub_status": "proposal_sent"})
    web.post(f"/companies/{cid_w}/mark-lead")
    assert web.post(f"/pipeline/enquiries/{eid_w}/status", data={"status": "won"}, follow_redirects=False).status_code == 303
    html = web.get("/pipeline?tab=leads").text
    keys = re.findall(r'<section class="board__col[^"]*"[^>]*data-stage="(\w+)"', html)
    assert keys == ["meeting_scheduled", "proposal_sent", "won"]
    assert f'id="company-{cid_m}"' in _col(html, "meeting_scheduled") and f'id="company-{cid_p}"' in _col(html, "proposal_sent")
    won = _col(html, "won")
    assert f'id="company-{cid_w}"' in won and "tab=clients" in won
    assert f'id="company-{cid_prospect}"' not in html
    assert _count(html, "leads") == 2 and _count(html, "clients") == 1 and _count(html, "prospects") == 1
    # recent only
    with session_scope() as db:
        db.execute(update(Client).values(created_at=utcnow() - timedelta(days=60)))
    assert f'id="company-{cid_w}"' not in web.get("/pipeline?tab=leads").text
    clients = web.get("/pipeline?tab=clients").text
    assert 'id="pipeline-list"' in clients and f'id="company-{cid_w}"' in clients and "Next follow-up" in clients
    assert f'id="company-{cid_m}"' not in clients


def test_filters_work_on_every_tab(web: TestClient) -> None:
    _, _, cid_a = _company("Acme Logistics", "P1")
    _, _, cid_b = _company("Beta Skills", "P2")
    web.post(f"/companies/{cid_a}/mark-lead")
    web.post(f"/companies/{cid_b}/mark-lead")
    leads = web.get("/pipeline?tab=leads&q=beta", headers=HX).text
    assert f'id="company-{cid_b}"' in leads and f'id="company-{cid_a}"' not in leads
    clients = web.get("/pipeline?tab=clients&priority=high")
    assert clients.status_code == 200
    enq = web.get("/pipeline?tab=enquiries&q=acme").text
    assert "Acme Logistics" in enq and "Beta Skills" not in enq


def test_enquiries_tab_groups_by_status_with_a_collapsed_lane_and_one_status_control(web: TestClient) -> None:
    eid, _, cid = _company("Acme Logistics", "P1")
    other = make_enquiry("P8", **{**triaged("genuine", "Buyer", summary="Second need"), "review_status": "accepted", "company": "Acme Logistics"})
    with session_scope() as db:
        prospects.ensure_for_enquiry(db, other)
    html = web.get("/pipeline").text
    keys = re.findall(r'<section class="board__col"[^>]*data-status="(\w+)"', html)
    assert keys[:8] == ["open", "contacted", "follow_up_1", "follow_up_2", "final_follow_up", "meeting_scheduled", "proposal_sent", "won"]
    assert "enquiry-closed-lane" not in html and 'id="enquiry-' + str(eid) + '"' in _col(html, "open")
    # every enquiry card offers the same statuses, plain labels, nothing disabled or "(auto)"
    card = re.search(rf'<article class="pcard" id="enquiry-{eid}".*?</article>', html, re.S).group(0)  # type: ignore[union-attr]
    other_card = re.search(rf'<article class="pcard" id="enquiry-{other}".*?</article>', html, re.S).group(0)  # type: ignore[union-attr]
    opts = [re.findall(r'<option value="(\w*)"', c) for c in (card, other_card)]
    assert opts[0] == opts[1] == list(deals_service.SETTABLE)
    assert "(auto)" not in card and "disabled" not in card and ">Open<" in card
    r = web.post(f"/pipeline/enquiries/{eid}/status", data={"status": "follow_up_1", "next": "/pipeline?tab=enquiries"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pipeline?tab=enquiries"
    with session_scope() as db:
        assert db.scalar(select(Prospect.stage)) == "follow_up_1"
    prospects_html = web.get("/pipeline?tab=prospects").text
    assert f'id="company-{cid}"' in _col(prospects_html, "follow_up_1")
    # the second enquiry of the same new prospect moves it forward too, never back
    ok = web.post(f"/pipeline/enquiries/{other}/status", data={"status": "final_follow_up", "next": "/pipeline"}, follow_redirects=True)
    assert "flash--error" not in ok.text
    with session_scope() as db:
        assert db.scalar(select(Prospect.stage)) == "final_follow_up"
    web.post(f"/pipeline/enquiries/{eid}/status", data={"status": "contacted", "next": "/pipeline"})
    with session_scope() as db:
        assert db.scalar(select(Prospect.stage)) == "final_follow_up" and db.scalar(select(Enquiry.deal_status).where(Enquiry.id == eid)) == "contacted"
    # parked statuses live in a collapsed lane
    assert web.post(f"/pipeline/enquiries/{other}/status", data={"status": "on_hold", "next": "/pipeline"}).status_code in (200, 303)
    html = web.get("/pipeline").text
    assert 'id="enquiry-closed-lane"' in html and f'id="enquiry-{other}"' in html.split('id="enquiry-closed-lane"', 1)[1]
    with session_scope() as db:
        assert db.scalar(select(Enquiry.deal_status).where(Enquiry.id == other)) == "on_hold"

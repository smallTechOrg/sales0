"""E4 through the real app: the priority chip on board cards / list rows / prospect page, the inline edit, and the
priority filter + sort (combined with the stage / characteristic filters)."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sales0.db.engine import session_scope
from sales0.db.models import Prospect
from sales0.llm import set_llm_client
from sales0.services.crm import prospects
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, make_enquiry, triaged
from tests.support.fake_llm import FakeLLMClient


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    set_llm_client(FakeLLMClient(responder=lambda req: {"subject": "s", "body": "b"}))
    with TestClient(create_app()) as c:
        yield c


def _prospect(post: str, company: str, prio: str) -> int:
    fields = triaged("genuine", "Buyer", summary=f"{company} - workshop")
    fields.update(review_status="accepted", company=company, priority=prio,
                  author_profile_url=f"https://www.linkedin.com/in/person-{post.lower()}/")
    eid = make_enquiry(post, **fields)
    with session_scope() as db:
        return prospects.ensure_for_enquiry(db, eid).id


def test_priority_chips_filters_and_inline_edit(web: TestClient) -> None:
    hi = _prospect("P1", "Hi Co", "high")
    lo = _prospect("P2", "Lo Co", "low")
    page = web.get("/pipeline").text
    assert re.search(r'data-priority="high"[^>]*>High<', page) and re.search(r'data-priority="low"[^>]*>Low<', page)
    assert 'id="f-priority"' in page
    only_high = web.get("/pipeline?priority=high").text
    assert "Hi Co" in only_high and "Lo Co" not in only_high
    assert "Lo Co" in web.get("/pipeline?priority=low&view=list&stage=new").text
    assert "Hi Co" not in web.get("/pipeline?priority=low&view=list&stage=new").text
    order = web.get("/pipeline?view=list&sort=priority").text
    assert order.index("Hi Co") < order.index("Lo Co")
    # prospect page: chip + editable select
    detail = web.get(f"/pipeline/prospects/{lo}").text
    assert 'id="priority-select"' in detail and "Automatic (Low)" in detail
    r = web.post(f"/pipeline/prospects/{lo}/priority", data={"priority": "high"}, headers=HX)
    assert r.status_code == 200 and "Priority set to High" in r.text
    with session_scope() as db:
        p = db.get(Prospect, lo)
        assert (p.priority, p.priority_source) == ("high", "manual")
    assert "(set by hand)" in web.get(f"/pipeline/prospects/{lo}").text
    r = web.post(f"/pipeline/prospects/{lo}/priority", data={"priority": ""}, headers=HX)
    assert "automatic again" in r.text
    bad = web.post(f"/pipeline/prospects/{lo}/priority", data={"priority": "urgent"}, headers=HX)
    assert "Priority must be High, Medium or Low" in bad.text
    # clients/leads tabs accept the same filter
    assert web.get("/pipeline?tab=leads&priority=high").status_code == 200
    assert web.get("/pipeline?tab=clients&priority=high&sort=priority").status_code == 200
    assert web.get(f"/pipeline/prospects/{hi}").status_code == 200

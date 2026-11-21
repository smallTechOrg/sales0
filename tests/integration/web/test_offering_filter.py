"""SMA-32: the global offering filter in the page header. Picking an offering sets the ``sales0_offering`` cookie and
every list (Inbox, Pipeline tabs, Companies, Analytics) shows only that offering until it is cleared; a page's own
explicit ``offering`` param still wins; an unknown cookie is ignored."""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Offering, Prospect
from sales0.services.crm import company_offerings, prospects
from sales0.web.offering_filter import COOKIE_NAME
from tests.integration.web.conftest import HX, make_enquiry, triaged


def _offering_id(name: str) -> int:
    with session_scope() as db:
        return int(db.scalar(select(Offering.id).where(Offering.name == name)))


@pytest.fixture
def offerings() -> tuple[int, int]:
    return _offering_id("AI corporate workshops"), _offering_id("Products")


def _company(name: str, offering_id: int) -> int:
    """A cold-list company (Prospects board) tagged with ``offering_id``."""
    with session_scope() as db:
        v = prospects.create_manual(db, name=f"Person at {name}", company=name, email=f"p@{name.lower().replace(' ', '')}.example")
        cid = int(db.get(Prospect, v.id).company_id)
        company_offerings.set_for(db, cid, [offering_id])
        return cid


def _accepted_enquiry(name: str, offering_id: int) -> int:
    """An accepted enquiry for ``offering_id`` whose company is New (Enquiries board)."""
    fields = triaged("genuine", "Buyer", offering_id=offering_id, summary=f"{name} - workshop")
    fields.update(review_status="accepted", company=name, contact_person="Priya")
    eid = make_enquiry("P1", text=f"We need training for {name}.", **fields)
    with session_scope() as db:
        prospects.ensure_for_enquiry(db, eid)
    return eid


def _review_enquiry(text: str, offering_id: int) -> int:
    return make_enquiry("P1", text=text, **triaged("genuine", "Buyer", offering_id=offering_id, summary=text))


def _set(client: TestClient, offering_id: int | str, next_url: str = "/pipeline") -> None:
    r = client.post("/offering-filter", data={"offering_id": str(offering_id), "next": next_url}, follow_redirects=False)
    assert r.status_code == 303


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _count(html: str, key: str) -> int:
    m = re.search(rf'id="tab-{key}"[^>]*>[^<]*<span class="count-pill">(\d+)</span>', html)
    assert m, key
    return int(m.group(1))


# ---------------------------------------------------------------------- the route + header
def test_header_selector_lists_offerings_and_works_without_js(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, prod = offerings
    with session_scope() as db:
        db.get(Offering, prod).active = False
    html = unescape(client.get("/pipeline?tab=prospects&q=x").text)
    form = html.split('id="offering-filter"', 1)[1].split("</form>", 1)[0]
    assert 'action="/offering-filter"' in html and 'name="offering_id" data-autosubmit' in form
    assert '<option value="" selected>All offerings</option>' in form
    assert f'<option value="{ws}">AI corporate workshops</option>' in form and "Products (off)" in form
    assert "<noscript><button" in form  # no JS: a submit button
    assert 'name="next" value="/pipeline?tab=prospects&amp;q=x"' in client.get("/pipeline?tab=prospects&q=x").text
    assert 'id="offering-filter-bar"' not in html  # no chip while no filter is on
    assert "onchange" not in form and "<script" not in form  # CSP: no inline JS


def test_set_and_clear_cookie(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, _prod = offerings
    r = client.post("/offering-filter", data={"offering_id": str(ws), "next": "/companies?q=a&offering=9&page=3"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/companies?q=a"  # page's own offering + page dropped
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE_NAME}={ws}" in cookie and "samesite=lax" in cookie.lower() and "Path=/" in cookie
    html = unescape(client.get("/inbox").text)
    bar = html.split('id="offering-filter-bar"', 1)[1].split("</div>", 1)[0]
    assert "Filtered: <strong>AI corporate workshops</strong>" in bar and 'id="offering-filter-clear"' in bar
    assert f'<option value="{ws}" selected>AI corporate workshops</option>' in html
    for clear in ("", "all", "999999"):  # "All offerings", the sentinel and an unknown id all clear it
        _set(client, ws)
        r = client.post("/offering-filter", data={"offering_id": clear, "next": "/inbox"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/inbox"
        assert f'{COOKIE_NAME}=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
        assert COOKIE_NAME not in client.cookies
        assert 'id="offering-filter-bar"' not in client.get("/inbox").text


@pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)", ""])
def test_next_is_open_redirect_safe(client: TestClient, offerings: tuple[int, int], bad: str) -> None:
    r = client.post("/offering-filter", data={"offering_id": str(offerings[0]), "next": bad}, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/") and not location.startswith("//") and "evil" not in location


def test_cross_site_post_is_rejected(client: TestClient, offerings: tuple[int, int]) -> None:
    r = client.post("/offering-filter", data={"offering_id": str(offerings[0]), "next": "/inbox"},
                    headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and COOKIE_NAME not in client.cookies


# ---------------------------------------------------------------------- pages filtered by the cookie
def test_pipeline_tabs_filtered_by_cookie_and_counts_match(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, prod = offerings
    a, b = _company("Alpha Works", ws), _company("Beta Goods", prod)
    e_ws, e_prod = _accepted_enquiry("Gamma Labs", ws), _accepted_enquiry("Delta Retail", prod)
    before = client.get("/pipeline?tab=prospects").text
    assert _count(before, "prospects") == 2 and _count(before, "enquiries") == 2
    _set(client, ws)
    html = client.get("/pipeline?tab=prospects").text
    main = _main(html)
    assert f'id="company-{a}"' in main and f'id="company-{b}"' not in main
    assert _count(html, "prospects") == 1 and _count(html, "enquiries") == 1
    assert f'<option value="{ws}" selected>' in html.split('id="f-offering"', 1)[1].split("</select>", 1)[0]
    enq = _main(client.get("/pipeline?tab=enquiries").text)
    assert "Gamma Labs" in enq and "Delta Retail" not in enq
    # the htmx body swap uses the cookie too
    frag = client.get("/pipeline?tab=prospects&q=", headers=HX).text
    assert f'id="company-{a}"' in frag and f'id="company-{b}"' not in frag
    lst = _main(client.get("/pipeline?tab=prospects&layout=list").text)
    assert "Alpha Works" in lst and "Beta Goods" not in lst
    assert e_ws and e_prod


def test_explicit_param_overrides_cookie(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, prod = offerings
    a, b = _company("Alpha Works", ws), _company("Beta Goods", prod)
    _set(client, ws)
    main = _main(client.get(f"/pipeline?tab=prospects&offering={prod}").text)
    assert f'id="company-{b}"' in main and f'id="company-{a}"' not in main
    both = _main(client.get("/pipeline?tab=prospects&offering=all").text)
    assert f'id="company-{a}"' in both and f'id="company-{b}"' in both
    comp = _main(client.get(f"/companies?offering={prod}").text)
    assert "Beta Goods" in comp and "Alpha Works" not in comp
    comp_all = _main(client.get("/companies?offering=all").text)
    assert "Beta Goods" in comp_all and "Alpha Works" in comp_all
    # the page's "Any" option keeps meaning every offering while the global filter is on
    assert '<option value="all" selected>Any</option>' in comp_all


def test_companies_filtered_by_cookie(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, prod = offerings
    _company("Alpha Works", ws)
    _company("Beta Goods", prod)
    _set(client, prod)
    main = _main(client.get("/companies").text)
    assert "Beta Goods" in main and "Alpha Works" not in main
    frag = client.get("/companies?q=", headers={**HX, "HX-Target": "company-results"}).text
    assert "Beta Goods" in frag and "Alpha Works" not in frag
    assert f'name="offering" value="{prod}"' in frag  # "Research all matching" carries the filter


def test_inbox_filtered_by_cookie_with_matching_counts(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, prod = offerings
    r1 = _review_enquiry("Workshop needed for our managers in Pune.", ws)
    r2 = _review_enquiry("Looking for a product licence for our staff.", prod)
    acc = make_enquiry("P1", text="Accepted workshop enquiry text.", **triaged("genuine", "Buyer", offering_id=ws),
                       review_status="accepted")
    html = client.get("/inbox").text
    assert f'id="enquiry-{r1}"' in html and f'id="enquiry-{r2}"' in html and '<span data-count="review">2</span>' in html
    _set(client, prod, "/inbox")
    html = client.get("/inbox").text
    assert f'id="enquiry-{r2}"' in html and f'id="enquiry-{r1}"' not in html
    assert '<span data-count="review">1</span>' in html and '<span data-count="accepted">0</span>' in html
    _set(client, ws, "/inbox")
    html = client.get("/inbox?tab=accepted").text
    assert f'id="enquiry-{acc}"' in html and '<span data-count="accepted">1</span>' in html
    assert '<span data-count="review">1</span>' in html


def test_analytics_uses_cookie_as_default(client: TestClient, offerings: tuple[int, int]) -> None:
    ws, prod = offerings
    _set(client, ws)
    html = client.get("/metrics/analytics").text
    select_html = html.split('id="an-offering"', 1)[1].split("</select>", 1)[0]
    assert f'<option value="{ws}" selected>' in select_html
    assert "that are a fit for the chosen offering" in html
    html = client.get(f"/metrics/analytics?offering={prod}").text
    assert f'<option value="{prod}" selected>' in html.split('id="an-offering"', 1)[1].split("</select>", 1)[0]
    html = client.get("/metrics/analytics?offering=all").text
    assert "that are a fit for the chosen offering" not in html


@pytest.mark.parametrize("bad", ["999999", "abc", "-1", "0", "99999999999999999999"])
def test_invalid_cookie_is_ignored(client: TestClient, offerings: tuple[int, int], bad: str) -> None:
    ws, prod = offerings
    a, b = _company("Alpha Works", ws), _company("Beta Goods", prod)
    client.cookies.set(COOKIE_NAME, bad)
    html = client.get("/pipeline?tab=prospects").text
    assert html and 'id="offering-filter-bar"' not in html
    main = _main(html)
    assert f'id="company-{a}"' in main and f'id="company-{b}"' in main
    assert client.get("/companies").status_code == 200 and client.get("/inbox").status_code == 200
    assert "Alpha Works" in _main(client.get("/companies").text)

"""Phase 1.5: opportunity_type chip on inbox cards; direct buyers sort first within a priority."""

from __future__ import annotations

import re
from html import unescape

from fastapi.testclient import TestClient

from tests.integration.web.conftest import make_enquiry, triaged


def _ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'<article id="enquiry-(\d+)"', html)]


def test_chip_shows_per_opportunity_type_and_direct_buyer_sorts_first(client: TestClient) -> None:
    agency = make_enquiry("P1", text="Agency wants a GenAI workshop provider for a client", **triaged("genuine", "agency", priority="medium", ai_opportunity_type="agency_requirement"))
    direct = make_enquiry("P1", text="Acme wants a GenAI workshop for its managers", added_offset_s=-60, **triaged("genuine", "buyer", priority="medium", ai_opportunity_type="direct_buyer"))
    other = make_enquiry("P1", text="Tells a story about trainers", added_offset_s=-120, **triaged("genuine", "x", priority="medium", ai_opportunity_type="other"))
    high_agency = make_enquiry("P1", text="High priority agency requirement", added_offset_s=-180, **triaged("genuine", "h", priority="high", ai_opportunity_type="agency_requirement"))
    html = unescape(client.get("/inbox").text)
    order = _ids(html)
    assert order[0] == high_agency  # priority still dominates
    assert order.index(direct) < order.index(agency)  # direct buyer first although older
    card = html[html.index(f'<article id="enquiry-{direct}"') :]
    assert 'data-opportunity="direct_buyer">Direct buyer</span>' in card
    assert 'data-opportunity="agency_requirement">Agency requirement</span>' in html
    other_card = html[html.index(f'<article id="enquiry-{other}"') :].split("</article>", 1)[0]
    assert "data-opportunity" not in other_card  # `other` shows no chip

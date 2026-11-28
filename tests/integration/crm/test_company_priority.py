"""SMA-60 / SMA-57: a company has ONE priority - its primary prospect's, the one its page shows and sets - and every
board, list, filter and sort uses it (the Enquiries board used each enquiry's own AI priority, so a company set to Low
on its page still showed High there)."""

from __future__ import annotations

from sales0.db.engine import session_scope
from sales0.db.models import Prospect
from sales0.services.crm import company_pipeline, companies, lifecycle, priority, prospects
from tests.integration.web.conftest import make_enquiry, triaged


def _accepted(post: str, company: str, prio: str) -> int:
    fields = {**triaged("genuine", "Buyer", summary=f"Need a workshop ({company})", priority=prio), "review_status": "accepted",
              "company": company}
    return make_enquiry(post, text=f"We need a corporate GenAI workshop for {company} managers", **fields)


def _enquiry_cards(db):  # type: ignore[no-untyped-def]
    return [c for col in company_pipeline.enquiry_board(db).columns for c in col.cards]


def test_setting_a_company_to_low_shows_low_on_the_enquiries_board(db_url: str) -> None:
    eid = _accepted("P1", "Hot Co", "high")
    with session_scope() as db:
        pid = prospects.ensure_for_enquiry(db, eid).id
        company_id = db.get(Prospect, pid).company_id
        assert [c.priority for c in _enquiry_cards(db)] == ["high"]
        priority.set_priority(db, lifecycle.primary_prospect(db, company_id).id, "low")  # as on the company page
    with session_scope() as db:
        [card] = _enquiry_cards(db)
        assert card.priority == "low" and card.company_card is not None and card.company_card.priority == "low"
        assert card.company_card.priority_source == "manual"
        # the filter follows what is shown
        assert [c.id for col in company_pipeline.enquiry_board(db, priority_filter="low").columns for c in col.cards] == [eid]
        assert not [c for col in company_pipeline.enquiry_board(db, priority_filter="high").columns for c in col.cards]


def test_the_company_card_shows_the_primary_prospects_priority_not_the_highest(db_url: str) -> None:
    eid = _accepted("P1", "Two Rows Co", "high")
    with session_scope() as db:
        first = db.get(Prospect, prospects.ensure_for_enquiry(db, eid).id)
        company_id = first.company_id
        first.status, first.closed_at = "closed", first.updated_at  # an old closed row keeps "high"
        second = Prospect(company_id=company_id, name="Two Rows Co", stage="contacted", status="open", priority="low",
                          priority_source="manual")
        db.add(second)
        db.flush()
        assert lifecycle.primary_prospect(db, company_id).id == second.id
        [card] = company_pipeline.cards_for(db, [lifecycle.company_row(db, company_id)])
        assert card.priority == "low"


def test_priority_comes_first_on_the_boards_and_the_companies_page(db_url: str) -> None:
    ids = {name: _accepted(post, name, prio) for post, name, prio in (("P1", "Low Co", "low"), ("P2", "High Co", "high"),
                                                                       ("P3", "Mid Co", "medium"))}
    with session_scope() as db:
        for eid in ids.values():
            prospects.ensure_for_enquiry(db, eid)
    with session_scope() as db:
        new_col = next(c for c in company_pipeline.enquiry_board(db).columns if c.cards)
        assert [c.priority for c in new_col.cards] == ["high", "medium", "low"]
        names = [c.name for c in companies.list_companies(db).items]
        assert names.index("High Co") < names.index("Mid Co") < names.index("Low Co")

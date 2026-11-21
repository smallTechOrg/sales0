"""SMA-33: enquiry companies and outreach companies take different paths. A company that came in through an accepted
enquiry stays on the Enquiries board while it is a prospect (New .. Final follow-up); a company we reach out to
(added by hand, a cold list, or put at Prospect: New by a person, or reopened) is on the Prospects board."""

from __future__ import annotations

from sales0.db.engine import session_scope
from sales0.db.models import Company, Prospect
from sales0.services.crm import company_pipeline, lifecycle, prospects
from tests.integration.web.conftest import make_enquiry, triaged


def _board_ids(db, tab: str) -> set[int]:  # type: ignore[no-untyped-def]
    return {card.id for col in company_pipeline.tab_board(db, tab).columns for card in col.cards}


def _enquiry_company_ids(db) -> set[int]:  # type: ignore[no-untyped-def]
    return {c.company_id for col in company_pipeline.enquiry_board(db).columns for c in col.cards}


def test_paths(db_url: str) -> None:
    eid = make_enquiry("P1", **{**triaged("genuine", "Buyer", summary="Need a workshop"), "review_status": "accepted", "company": "Inbound Co"})
    with session_scope() as db:
        inbound = db.get(Prospect, prospects.ensure_for_enquiry(db, eid).id).company_id
        cold = db.get(Prospect, prospects.create_manual(db, name="Cold Co").id).company_id
    with session_scope() as db:
        assert (db.get(Company, inbound).pipeline_path, db.get(Company, cold).pipeline_path) == ("enquiry", "outreach")
        assert inbound in _enquiry_company_ids(db) and inbound not in _board_ids(db, "prospects")
        assert cold in _board_ids(db, "prospects")  # Prospects: New straight away
        for stage in ("contacted", "follow_up", "final_follow_up"):  # the enquiry path keeps it on the Enquiries board
            lifecycle.move_to(db, inbound, f"prospect:{stage}")
            assert inbound in _enquiry_company_ids(db) and inbound not in _board_ids(db, "prospects"), stage
        lifecycle.move_to(db, inbound, "prospect:new")  # a person puts it at Prospect: New -> outreach
        assert db.get(Company, inbound).pipeline_path == "outreach"
        assert inbound in _board_ids(db, "prospects") and inbound not in _enquiry_company_ids(db)
        assert company_pipeline.counts(db)["prospect"] == 2

"""``company_projection.derive``: what the ledger rows (prospects, leads, clients) say about the company's stage."""

from __future__ import annotations

from datetime import UTC, datetime

from sales0.db.company_projection import Rows, derive
from sales0.db.models.enums_crm import ENQUIRY_DEAL_STATUSES

T = datetime(2026, 10, 1, tzinfo=UTC)
T2 = datetime(2026, 10, 5, tzinfo=UTC)


def prospect(stage="new", status="open", reason=None, closed_at=None, source="app"):
    return {"stage": stage, "status": status, "closed_reason": reason, "closed_at": closed_at, "stage_source": source, "updated_at": T}


def lead(stage="meeting_scheduled", closed_at=None, source="app", proposal=None):
    return {"stage": stage, "closed_at": closed_at, "stage_source": source, "proposal_sent_on": proposal}


def test_no_ledger_row_leaves_the_company_alone() -> None:
    assert derive(Rows()) is None


def test_open_prospect_is_a_prospect_with_its_cadence_status() -> None:
    life = derive(Rows(prospects=[prospect("follow_up_1")]))
    assert (life.stage, life.sub_status, life.off_ramp) == ("prospect", "follow_up_1", None)


def test_the_most_advanced_open_thread_wins() -> None:
    life = derive(Rows(prospects=[prospect("contacted"), prospect("final_follow_up"), prospect("new")]))
    assert life.sub_status == "final_follow_up"


def test_open_lead_beats_prospects_and_proposal_beats_meeting() -> None:
    rows = Rows(prospects=[prospect("contacted", status="converted"), prospect("new")],
                leads=[lead("meeting_scheduled"), lead("proposal_sent")])
    life = derive(rows)
    assert (life.stage, life.sub_status) == ("lead", "proposal_sent")


def test_client_or_a_won_lead_wins_over_everything() -> None:
    assert derive(Rows(prospects=[prospect()], clients=1)).stage == "client"
    assert derive(Rows(leads=[lead("won", closed_at=T)])).stage == "client"
    assert derive(Rows(prospects=[prospect(status="closed", reason="lost", closed_at=T)], clients=1)).off_ramp is None


def test_closed_prospect_is_an_off_ramp_that_remembers_where_it_stood() -> None:
    gone = derive(Rows(prospects=[prospect("follow_up_2", status="closed", reason="no_response", closed_at=T)]))
    assert (gone.stage, gone.sub_status, gone.off_ramp, gone.resume_stage, gone.resume_sub_status) == (
        "prospect", "follow_up_2", "dormant", "prospect", "follow_up_2")
    no = derive(Rows(prospects=[prospect("contacted", status="closed", reason="not_interested", closed_at=T)]))
    assert no.off_ramp == "not_interested"
    assert derive(Rows(prospects=[prospect(status="closed", reason="enquiry_discarded", closed_at=T)])).off_ramp == "not_interested"
    assert derive(Rows(prospects=[prospect(status="closed", reason="sheet: Hold", closed_at=T)])).off_ramp == "dormant"


def test_lost_and_no_response_leads_are_off_ramps_from_the_lead_stage() -> None:
    lost = derive(Rows(prospects=[prospect(status="converted")], leads=[lead("lost", closed_at=T, proposal=T.date())]))
    assert (lost.stage, lost.sub_status, lost.off_ramp, lost.resume_stage) == ("lead", "proposal_sent", "not_interested", "lead")
    stale = derive(Rows(prospects=[prospect(status="converted")], leads=[lead("no_response", closed_at=T)]))
    assert (stale.off_ramp, stale.sub_status) == ("dormant", "meeting_scheduled")


def test_the_latest_closure_decides() -> None:
    life = derive(Rows(prospects=[prospect(status="closed", reason="not_interested", closed_at=T)],
                       leads=[lead("no_response", closed_at=T2)]))
    assert life.off_ramp == "dormant" and life.resume_stage == "lead"


def test_a_sheet_only_stage_is_marked_sheet_so_the_app_keeps_precedence() -> None:
    assert derive(Rows(prospects=[prospect("contacted", source="sheet")])).stage_source == "sheet"
    assert derive(Rows(prospects=[prospect("contacted", source="sheet"), prospect("new", source="app")])).stage_source == "app"


def test_one_enquiry_status_list() -> None:
    assert len(set(ENQUIRY_DEAL_STATUSES)) == 11 and ENQUIRY_DEAL_STATUSES[0] == "open"

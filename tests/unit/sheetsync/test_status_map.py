"""Pure status -> stage table (spec/data.md), one case per rule."""

from __future__ import annotations

from datetime import date

import pytest

from sales0.services.sheetsync.status_map import map_enquiry_status, map_person_status

D = date(2026, 7, 1)


@pytest.mark.parametrize("status", ["Won", "closed won", "  CLIENT ", "Project  Done", "delivered", "Confirmed"])
def test_rule1_won(status: str) -> None:
    o = map_enquiry_status(status)
    assert (o.prospect_status, o.lead_stage, o.prospect_stage, o.rule) == ("converted", "won", None, "1")


@pytest.mark.parametrize("status", ["Lost", "Rejected", "not interested", "Dropped", "declined"])
def test_rule2_lost_needs_contact_evidence(status: str) -> None:
    o = map_enquiry_status(status, date_email_sent=D)
    assert (o.prospect_status, o.lead_stage, o.rule) == ("converted", "lost", "2")
    # without evidence the word is not enough: first match wins, falls to rule 9 and is flagged
    o = map_enquiry_status(status)
    assert (o.prospect_stage, o.lead_stage, o.rule, o.unknown) == ("new", None, "9", True)


def test_rule2b_closed_with_proposal_is_lost_but_plain_closed_is_not() -> None:
    o = map_enquiry_status("Closed", date_proposal_sent=D)
    assert (o.lead_stage, o.prospect_status, o.rule) == ("lost", "converted", "2b")
    o = map_enquiry_status("Closed")
    assert (o.lead_stage, o.prospect_status, o.closed_reason, o.rule) == (None, "closed", "sheet: Closed", "8")
    # a closed row that only had an e-mail is a closed prospect, never a false lead / client
    o = map_enquiry_status("Closed", date_email_sent=D)
    assert o.lead_stage is None and o.prospect_status == "closed"


def test_rule3_no_response_depends_on_contact_evidence() -> None:
    o = map_enquiry_status("No Response", date_email_sent=D)
    assert (o.prospect_status, o.lead_stage) == ("converted", "no_response")
    o = map_enquiry_status("ghosted")
    assert (o.prospect_status, o.closed_reason, o.lead_stage) == ("closed", "no_response", None)


@pytest.mark.parametrize("status", ["Meeting Scheduled", "meeting", "call scheduled", "interested", "replied", "Responded"])
def test_rule4_meeting(status: str) -> None:
    assert map_enquiry_status(status).lead_stage == "meeting_scheduled"


def test_rule5_proposal_by_word_or_date() -> None:
    assert map_enquiry_status("Proposal Sent").lead_stage == "proposal_sent"
    assert map_enquiry_status("anything", date_proposal_sent=D).lead_stage == "proposal_sent"
    assert map_enquiry_status("", date_proposal_sent=D).prospect_status == "converted"


@pytest.mark.parametrize(
    ("status", "stage", "rule"),
    [("FU1", "follow_up_1", "6"), ("follow up 2", "follow_up_2", "6b"), ("Final follow up", "final_follow_up", "6c")],
)
def test_rule6_follow_ups(status: str, stage: str, rule: str) -> None:
    o = map_enquiry_status(status)
    assert (o.prospect_stage, o.prospect_status, o.lead_stage, o.rule) == (stage, "open", None, rule)


def test_rule7_contacted_by_word_or_date() -> None:
    assert map_enquiry_status("Email Sent").prospect_stage == "contacted"
    o = map_enquiry_status("", date_email_sent=D)
    assert (o.prospect_stage, o.contacted, o.lead_stage) == ("contacted", True, None)


@pytest.mark.parametrize("status", ["inactive", "On Hold", "parked", "cancelled", "NA"])
def test_rule8_closed(status: str) -> None:
    o = map_enquiry_status(status)
    assert o.prospect_status == "closed" and o.closed_reason == f"sheet: {status}" and o.lead_stage is None


@pytest.mark.parametrize("status", [None, "", "New", "open", "Pending", "to do"])
def test_rule9_neutral(status: str | None) -> None:
    o = map_enquiry_status(status)
    assert (o.prospect_stage, o.prospect_status, o.rule, o.unknown) == ("new", "open", "9", False)


def test_unknown_status_is_new_and_flagged() -> None:
    o = map_enquiry_status("Waiting for the CEO")
    assert (o.prospect_stage, o.rule, o.unknown) == ("new", "9", True)


def test_person_statuses() -> None:
    assert map_person_status("Message sent").prospect_stage == "contacted"
    assert map_person_status("Connection request sent").contacted is True
    assert map_person_status("closed").closed_reason == "sheet: closed"
    assert map_person_status("closed").prospect_status == "closed"
    for blank in (None, "", "something else"):
        o = map_person_status(blank)
        assert (o.prospect_stage, o.prospect_status) == ("new", "open")


# ---- binding rule: a Lead exists only after contact AND a response (no substring matching)
@pytest.mark.parametrize(
    "status", ["No proposal yet", "Proposal to be sent", "Not interested in a proposal", "proposal pending", "Awaiting proposal"]
)
def test_proposal_words_in_negated_phrases_make_no_lead(status: str) -> None:
    o = map_enquiry_status(status)
    assert o.lead_stage is None and o.prospect_status == "open" and o.prospect_stage == "new"


@pytest.mark.parametrize("status", ["Proposal Sent", "proposal submitted", "Quotation sent", "Proposal-Sent!"])
def test_explicit_proposal_phrases_open_a_proposal_lead(status: str) -> None:
    assert map_enquiry_status(status).lead_stage == "proposal_sent"


@pytest.mark.parametrize("status", ["Hot", "warm", "In progress", "ongoing", "in discussion", "Negotiation"])
def test_temperature_words_do_not_create_a_meeting_lead(status: str) -> None:
    o = map_enquiry_status(status)
    assert o.lead_stage is None and o.prospect_stage == "new"


def test_proposal_date_implies_a_response_even_with_a_neutral_or_negated_status() -> None:
    assert map_enquiry_status("New", date_proposal_sent=D).lead_stage == "proposal_sent"
    assert map_enquiry_status("No proposal yet", date_proposal_sent=D).lead_stage == "proposal_sent"
    assert map_enquiry_status("Closed", date_proposal_sent=D).lead_stage == "lost"


@pytest.mark.parametrize(
    "status", ["Message not sent", "To be sent", "Consent pending", "Not yet sent", "unsent", "Email not sent", "Not sent"]
)
def test_negated_sent_phrases_are_not_contacted(status: str) -> None:
    for o in (map_person_status(status), map_enquiry_status(status)):
        assert not o.contacted and o.prospect_stage == "new" and o.lead_stage is None
        assert not o.unknown  # understood, just not an action


def test_exact_contacted_phrases_still_work() -> None:
    for status in ("Message sent", "message  SENT", "Connection request sent", "Sent", "Email Sent."):
        assert map_person_status(status).contacted
        assert map_enquiry_status(status).contacted


def test_unknown_statuses_map_to_new_and_are_flagged() -> None:
    for fn in (map_person_status, map_enquiry_status):
        o = fn("Bananas")
        assert o.unknown and o.prospect_stage == "new" and o.lead_stage is None
        assert not fn("").unknown and not fn("New").unknown

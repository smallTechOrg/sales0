"""Pure sheet Status -> CRM stage mapping (spec/data.md "Sheet status -> CRM stage mapping").

No database, no I/O. ``map_enquiry_status`` is used for Active Enquiries rows and
``map_person_status`` for Outreach / Prospects / Working Sheet rows. The raw sheet text is
never altered here (it lives in ``sheet_status``); this only decides what the CRM should
show. The caller applies the outcome only when ``stage_source != 'app'``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_WS = re.compile(r"\s+")


def norm_status(value: str | None) -> str:
    """``casefold()`` of the trimmed cell with inner whitespace collapsed."""
    return _WS.sub(" ", (value or "").strip()).casefold()


@dataclass(frozen=True)
class StatusOutcome:
    """What a sheet status means for the CRM.

    ``prospect_stage`` None = keep the prospect's current stage (a brand-new prospect then
    starts at ``new``). ``prospect_status`` is open | converted | closed.
    """

    prospect_stage: str | None
    prospect_status: str
    closed_reason: str | None = None
    lead_stage: str | None = None
    contacted: bool = False  # the prospect should show as contacted (estimated, from the sheet)
    rule: str = "9"
    unknown: bool = False  # a non-blank status that matched nothing (logged once per value)


WON = frozenset({"won", "closed won", "converted", "client", "confirmed", "project done", "completed", "delivered"})
LOST = frozenset({"lost", "closed lost", "rejected", "declined", "not interested", "dropped"})
NO_RESPONSE = frozenset({"no response", "no reply", "not responding", "ghosted"})
MEETING = frozenset({
    "meeting scheduled", "meeting", "call scheduled", "demo scheduled", "in discussion", "discussion", "negotiation",
    "interested", "responded", "replied", "in progress", "ongoing", "hot", "warm",
})
FOLLOW_1 = frozenset({"follow up 1", "followup 1", "fu1", "first follow up", "follow-up 1"})
FOLLOW_2 = frozenset({"follow up 2", "followup 2", "fu2", "second follow up", "follow-up 2"})
FOLLOW_FINAL = frozenset({"final follow up", "last follow up", "final follow-up", "last follow-up"})
CONTACTED = frozenset({"contacted", "message sent", "email sent", "mail sent", "connection request sent", "sent"})
CLOSED = frozenset({"closed", "close", "inactive", "hold", "on hold", "parked", "cancelled", "na"})
NEUTRAL = frozenset({"", "new", "open", "pending", "to do"})
CLOSE_WORDS = frozenset({"closed", "close"})

LEAD_OUTCOME_STAGES = frozenset({"won", "lost", "no_response"})


def map_enquiry_status(
    sheet_status: str | None, *, date_email_sent: date | None = None, date_proposal_sent: date | None = None
) -> StatusOutcome:
    s = norm_status(sheet_status)
    raw = (sheet_status or "").strip()
    evidence = date_email_sent is not None or date_proposal_sent is not None
    if s in WON:
        return StatusOutcome(None, "converted", lead_stage="won", rule="1")
    if s in LOST and evidence:
        return StatusOutcome(None, "converted", lead_stage="lost", rule="2")
    if s in CLOSE_WORDS and date_proposal_sent is not None:
        return StatusOutcome(None, "converted", lead_stage="lost", rule="2b")
    if s in NO_RESPONSE:
        if evidence:
            return StatusOutcome(None, "converted", lead_stage="no_response", rule="3")
        return StatusOutcome(None, "closed", closed_reason="no_response", rule="3")
    if s in MEETING:
        return StatusOutcome(None, "converted", lead_stage="meeting_scheduled", rule="4")
    if "proposal" in s or date_proposal_sent is not None:
        return StatusOutcome(None, "converted", lead_stage="proposal_sent", rule="5")
    if s in FOLLOW_1:
        return StatusOutcome("follow_up_1", "open", rule="6")
    if s in FOLLOW_2:
        return StatusOutcome("follow_up_2", "open", rule="6b")
    if s in FOLLOW_FINAL:
        return StatusOutcome("final_follow_up", "open", rule="6c")
    # Deviation (documented in NOTES): an explicit "closed"-family word beats the date-only
    # inference of rule 7, so a Closed row that once had an e-mail is a closed prospect.
    if s in CONTACTED or (date_email_sent is not None and s not in CLOSED):
        return StatusOutcome("contacted", "open", contacted=True, rule="7")
    if s in CLOSED:
        return StatusOutcome(None, "closed", closed_reason=f"sheet: {raw}"[:60], rule="8")
    return StatusOutcome("new", "open", rule="9", unknown=s not in NEUTRAL)


def map_person_status(sheet_status: str | None) -> StatusOutcome:
    s = norm_status(sheet_status)
    raw = (sheet_status or "").strip()
    if s == "closed":
        return StatusOutcome(None, "closed", closed_reason=f"sheet: {raw}"[:60], rule="person-closed")
    if "sent" in s:
        return StatusOutcome("contacted", "open", contacted=True, rule="person-contacted")
    return StatusOutcome("new", "open", rule="person-new")

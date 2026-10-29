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


_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)


def norm_status(value: str | None) -> str:
    """``casefold()`` of the trimmed cell with inner whitespace collapsed."""
    return _WS.sub(" ", (value or "").strip()).casefold()


def phrase(value: str | None) -> str:
    """Tokenised, punctuation-free form used for exact-phrase matching ("Follow-up 1!" -> "follow up 1")."""
    return " ".join(_TOKEN.findall(norm_status(value).replace("-", " ").replace("_", " ")))


def negated(value: str | None) -> bool:
    return bool(set(phrase(value).split()) & NEGATORS)


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


NEGATORS = frozenset({"no", "not", "yet", "to", "be", "pending", "awaiting", "unsent", "without", "never", "soon", "will", "need", "needs", "draft", "planned"})  # noqa: E501
WON = frozenset({"won", "closed won", "converted", "client", "confirmed", "project done", "completed", "delivered"})
LOST = frozenset({"lost", "closed lost", "rejected", "declined", "not interested", "dropped"})
NO_RESPONSE = frozenset({"no response", "no reply", "not responding", "ghosted"})
MEETING = frozenset({"meeting scheduled", "meeting", "call scheduled", "interested", "replied", "responded"})
PROPOSAL = frozenset({"proposal sent", "sent proposal", "proposal submitted", "proposal shared", "quotation sent", "quote sent"})
FOLLOW_1 = frozenset({"follow up 1", "followup 1", "fu1", "first follow up"})
FOLLOW_2 = frozenset({"follow up 2", "followup 2", "fu2", "second follow up"})
FOLLOW_FINAL = frozenset({"final follow up", "last follow up"})
CONTACTED = frozenset({"contacted", "message sent", "email sent", "mail sent", "emailed", "messaged", "connection request sent", "sent"})
CLOSED = frozenset({"closed", "close", "inactive", "hold", "on hold", "parked", "cancelled", "na"})
NEUTRAL = frozenset({"", "new", "open", "pending", "to do"})
# Known, deliberately non-actionable: never create a lead or mark contact (temperature words).
TEMPERATURE = frozenset({"hot", "warm", "in progress", "ongoing"})
CLOSE_WORDS = frozenset({"closed", "close"})

LEAD_OUTCOME_STAGES = frozenset({"won", "lost", "no_response"})


def _known_negative(p: str) -> bool:
    """A negated phrase about sending / proposals / meetings: understood, maps to New, not "unknown"."""
    return negated(p) and bool(set(p.split()) & {"sent", "unsent", "proposal", "send", "meeting", "response", "reply", "consent"})


def map_enquiry_status(
    sheet_status: str | None, *, date_email_sent: date | None = None, date_proposal_sent: date | None = None
) -> StatusOutcome:
    s = phrase(sheet_status)
    raw = (sheet_status or "").strip()
    evidence = date_email_sent is not None or date_proposal_sent is not None
    neg = negated(s)
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
    if s in MEETING and not neg:
        return StatusOutcome(None, "converted", lead_stage="meeting_scheduled", rule="4")
    if (s in PROPOSAL and not neg) or date_proposal_sent is not None:
        return StatusOutcome(None, "converted", lead_stage="proposal_sent", rule="5")
    if s in FOLLOW_1:
        return StatusOutcome("follow_up_1", "open", rule="6")
    if s in FOLLOW_2:
        return StatusOutcome("follow_up_2", "open", rule="6b")
    if s in FOLLOW_FINAL:
        return StatusOutcome("final_follow_up", "open", rule="6c")
    # Deviation (documented in NOTES): an explicit "closed"-family word beats the date-only
    # inference of rule 7, so a Closed row that once had an e-mail is a closed prospect.
    if (s in CONTACTED and not neg) or (date_email_sent is not None and s not in CLOSED):
        return StatusOutcome("contacted", "open", contacted=True, rule="7")
    if s in CLOSED:
        return StatusOutcome(None, "closed", closed_reason=f"sheet: {raw}"[:60], rule="8")
    return StatusOutcome("new", "open", rule="9", unknown=s not in NEUTRAL and s not in TEMPERATURE and not _known_negative(s))


def map_person_status(sheet_status: str | None) -> StatusOutcome:
    s = phrase(sheet_status)
    raw = (sheet_status or "").strip()
    if s == "closed":
        return StatusOutcome(None, "closed", closed_reason=f"sheet: {raw}"[:60], rule="person-closed")
    if s in CONTACTED and not negated(s):
        return StatusOutcome("contacted", "open", contacted=True, rule="person-contacted")
    known = s in NEUTRAL or s in TEMPERATURE or s in MEETING or _known_negative(s)
    return StatusOutcome("new", "open", rule="person-new", unknown=not known)

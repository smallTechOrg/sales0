"""What each company status means, shown while a person picks one (the move popup, the "..." menu, tooltips).

Nothing here moves a company: statuses change only when a person moves the company (a board drag, a card's Move...
button or the "..." menu on the company page)."""

from __future__ import annotations

STATUS_HELP: dict[str, str] = {
    "new": "New: nobody has reached out yet.",
    "contacted": "Contacted: we reached out once.",
    "follow_up_1": "Follow-up 1: the first follow-up has gone out.",
    "follow_up_2": "Follow-up 2: the second follow-up has gone out.",
    "final_follow_up": "Final follow-up: the last follow-up; close the company if they never answer.",
    "meeting_scheduled": "Meeting scheduled: a meeting is booked. The company moves to the Leads board.",
    "assessment": "Assessment: they are assessing us (a trial, a test session, a review).",
    "proposal_sent": "Proposal sent: our proposal is with them (the date is recorded).",
    "interviews": "Interviews: trainer / team interviews after the proposal.",
    "on_hold": "On hold: paused on their side; it stays on the Leads board.",
    "active": "Client: they bought from us. Marked by mistake? Move it back to a Leads or Prospects status.",
}

__all__ = ["STATUS_HELP"]

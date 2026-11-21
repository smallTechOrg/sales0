"""What each company status means, shown while a person picks one (the move popup, the "..." menu, tooltips).

Nothing here moves a company: statuses change only when a person moves the company (a board drag, a card's Move...
button or the "..." menu on the company page)."""

from __future__ import annotations

STATUS_HELP: dict[str, str] = {
    "new": "New: nobody has reached out yet.",
    "contacted": "Contacted: we reached out once.",
    "follow_up": "Follow-up: we have followed up (once or more).",
    "final_follow_up": "Final follow-up: the last follow-up; close the company if they never answer.",
    "qualified": "Qualified: a new lead - they are interested and it is a real opportunity. The company moves to the Leads board.",
    "engaged": "Engaged: we are in active talks (meetings, an assessment, a proposal, interviews).",
    "on_hold": "On hold: paused on their side; it stays on the Leads board.",
    "active": "Customer: they bought from us. Marked by mistake? Move it back to a Leads or Prospects status.",
}

__all__ = ["STATUS_HELP"]

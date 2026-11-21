"""Pipeline vocabulary (pure): stage tuples, labels and ordering."""

from __future__ import annotations

from sales0.db.models.enums_crm import ENQUIRY_DEAL_STATUSES, LEAD_STAGES, PROSPECT_STAGES, RETIRED_STAGES

STAGE_LABELS: dict[str, str] = {
    "new": "New",
    "contacted": "Contacted",
    "follow_up": "Follow-up",
    "final_follow_up": "Final follow-up",
    "qualified": "Qualified",
    "engaged": "Engaged",
    "on_hold": "On hold",
    "won": "Won",
    "lost": "Lost",
    "no_response": "No response",
}
# SMA-17: retired names, still shown in old timeline entries
RETIRED_LABELS: dict[str, str] = {
    "follow_up_1": "Follow-up 1", "follow_up_2": "Follow-up 2", "meeting_scheduled": "Meeting scheduled",
    "assessment": "Assessment", "proposal_sent": "Proposal sent", "interviews": "Interviews",
}
# the enquiry deal-status labels (an accepted enquiry's own status), retired ones kept for history
DEAL_LABELS: dict[str, str] = {"open": "Open", **{s: STAGE_LABELS[s] for s in ENQUIRY_DEAL_STATUSES if s in STAGE_LABELS},
                               **RETIRED_LABELS}
TERMINAL_LEAD_STAGES = ("won", "lost", "no_response")
# Prospect stages rank 0-4, lead stages 10-14 (one total order, no collisions).
_RANK = {**{s: i for i, s in enumerate(PROSPECT_STAGES)}, **{s: 10 + i for i, s in enumerate(LEAD_STAGES)}}


def stage_rank(stage: str) -> int:
    try:
        return _RANK[RETIRED_STAGES.get(stage, stage)]
    except KeyError:
        raise ValueError(f"unknown stage {stage!r}") from None


def is_terminal_lead_stage(stage: str) -> bool:
    return stage in TERMINAL_LEAD_STAGES


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage) or RETIRED_LABELS.get(stage) or stage.replace("_", " ").capitalize()


__all__ = [
    "DEAL_LABELS",
    "LEAD_STAGES",
    "PROSPECT_STAGES",
    "RETIRED_LABELS",
    "STAGE_LABELS",
    "TERMINAL_LEAD_STAGES",
    "is_terminal_lead_stage",
    "stage_label",
    "stage_rank",
]

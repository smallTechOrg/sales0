"""Pipeline vocabulary (pure): stage tuples, labels and ordering."""

from __future__ import annotations

from sales0.db.models.enums_crm import LEAD_STAGES, PROSPECT_STAGES

STAGE_LABELS: dict[str, str] = {
    "new": "New",
    "contacted": "Contacted",
    "follow_up_1": "Follow-up 1",
    "follow_up_2": "Follow-up 2",
    "final_follow_up": "Final follow-up",
    "meeting_scheduled": "Meeting scheduled",
    "proposal_sent": "Proposal sent",
    "won": "Won",
    "lost": "Lost",
    "no_response": "No response",
}
TERMINAL_LEAD_STAGES = ("won", "lost", "no_response")
# Prospect stages rank 0-4, lead stages 10-14 (one total order, no collisions).
_RANK = {**{s: i for i, s in enumerate(PROSPECT_STAGES)}, **{s: 10 + i for i, s in enumerate(LEAD_STAGES)}}


def stage_rank(stage: str) -> int:
    try:
        return _RANK[stage]
    except KeyError:
        raise ValueError(f"unknown stage {stage!r}") from None


def is_terminal_lead_stage(stage: str) -> bool:
    return stage in TERMINAL_LEAD_STAGES


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize())


__all__ = [
    "LEAD_STAGES",
    "PROSPECT_STAGES",
    "STAGE_LABELS",
    "TERMINAL_LEAD_STAGES",
    "is_terminal_lead_stage",
    "stage_label",
    "stage_rank",
]

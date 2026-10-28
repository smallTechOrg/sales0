"""Stage vocabulary, actors and the frozen view models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sales0.db.models import TeamMember
from sales0.db.models.enums_crm import TIMELINE_KINDS
from sales0.services.crm import stages, views
from sales0.services.crm.actor import IMPORT, SHEET_SYNC, SYSTEM, TEAM, Actor
from sales0.services.crm.timeline import KIND_LABELS


def test_stage_tuples_and_labels_follow_the_lifecycle() -> None:
    assert stages.PROSPECT_STAGES == ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up")
    assert stages.LEAD_STAGES == ("meeting_scheduled", "proposal_sent", "won", "lost", "no_response")
    assert stages.STAGE_LABELS["follow_up_1"] == "Follow-up 1" and stages.STAGE_LABELS["meeting_scheduled"] == "Meeting scheduled"
    assert set(stages.STAGE_LABELS) == set(stages.PROSPECT_STAGES) | set(stages.LEAD_STAGES)
    assert stages.stage_label("final_follow_up") == "Final follow-up" and stages.stage_label("odd_thing") == "Odd thing"


def test_ranks_are_ordered_and_distinct() -> None:
    ranks = [stages.stage_rank(s) for s in (*stages.PROSPECT_STAGES, *stages.LEAD_STAGES)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 10
    assert stages.stage_rank("contacted") > stages.stage_rank("new")
    with pytest.raises(ValueError):
        stages.stage_rank("nope")


def test_terminal_lead_stages() -> None:
    assert [s for s in stages.LEAD_STAGES if stages.is_terminal_lead_stage(s)] == ["won", "lost", "no_response"]
    assert not stages.is_terminal_lead_stage("new")


def test_actors() -> None:
    assert (SYSTEM.label, SHEET_SYNC.label, IMPORT.label, TEAM.label) == ("system", "sheet-sync", "import", "team")
    assert Actor.of_member(None) is TEAM
    m = TeamMember(id=4, name="Priya", email="p@t.example")
    a = Actor.of_member(m)
    assert a == Actor("Priya", 4)
    with pytest.raises(Exception):
        a.label = "x"  # type: ignore[misc]  # frozen


def test_every_timeline_kind_has_a_label() -> None:
    assert set(KIND_LABELS) == set(TIMELINE_KINDS)


def test_views_are_frozen_and_prospect_detail_extends_the_view() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    v = views.ProspectView(id=1, name="A", kind="company_enquiry", stage="new", stage_label="New", status="open", source="enquiry", created_at=now)
    with pytest.raises(ValidationError):
        v.name = "B"  # type: ignore[misc]
    d = views.ProspectDetail(**v.model_dump())
    assert d.enquiries == [] and d.timeline == [] and d.lead is None
    assert views.ProspectPage().page_size == 50

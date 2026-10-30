"""Triage criteria (q14): the team-editable include / exclude rules of the triage prompt, and one immutable
snapshot per change (``triage_criteria_sets``) so a triage call (``llm_calls.criteria_version``) can be traced to
the exact rule set that was in its system prompt."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, true
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

CRITERION_KINDS: tuple[str, ...] = ("include", "exclude")
# seed = shipped with the app (the rules hard-coded up to triage-v3); team = added on the Criteria page;
# discard = created from a discard reason ("save as rule" on an inbox card)
CRITERION_SOURCES: tuple[str, ...] = ("seed", "team", "discard")


class TriageCriterion(Base):
    __tablename__ = "triage_criteria"
    __table_args__ = (
        in_check("kind", CRITERION_KINDS, "kind"),
        in_check("source", CRITERION_SOURCES, "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="team", server_default="team")
    from_enquiry_id: Mapped[int | None] = mapped_column(Integer)  # the enquiry whose discard reason became this rule
    # q22: the offering this rule belongs to (None = applies to every post). Rendered as "(only for offering X)" and
    # left out of the prompt while that offering is switched off.
    offering_id: Mapped[int | None] = mapped_column(ForeignKey("offerings.id", ondelete="SET NULL"), index=True)
    created_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    updated_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class TriageCriteriaSet(Base):
    """Immutable: the ACTIVE rules (kind + text, in prompt order) after a change. ``version`` increases by one."""

    __tablename__ = "triage_criteria_sets"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))
    actor_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)

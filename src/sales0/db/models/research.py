"""Per-prospect research, account summary and notes; the laptop worker's one-page tasks (migration q15).

* ``prospect_docs``  - versioned AI documents of a prospect. ``doc_type`` research (a company brief, JSON in
  ``data_json``) or summary (the account summary, text in ``body``). Every AI run AND every human edit is a new row
  (``version`` 1, 2, ... per prospect + doc type); the highest version is the current one. ``origin`` ai | human.
* ``prospect_notes`` - the team's own dated, authored notes on a prospect (add / edit / delete own).
* ``worker_tasks``   - a lightweight job for the laptop worker: ``company_page`` = visit ONE LinkedIn company page
  and upload its visible text. Claimed through ``/api/worker/claim`` by workers that advertise the capability.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sales0.db.base import Base
from sales0.db.models._types import UTCDateTime, in_check, now_utc

DOC_TYPES: tuple[str, ...] = ("research", "summary")
DOC_ORIGINS: tuple[str, ...] = ("ai", "human")
DOC_STATUSES: tuple[str, ...] = ("done", "partial", "failed")
NOTE_SOURCES: tuple[str, ...] = ("app", "migrated")
TASK_KINDS: tuple[str, ...] = ("company_page",)
TASK_STATUSES: tuple[str, ...] = ("queued", "claimed", "done", "failed", "cancelled")


class ProspectDoc(Base):
    __tablename__ = "prospect_docs"
    __table_args__ = (
        UniqueConstraint("prospect_id", "doc_type", "version", name="uq_prospect_docs_version"),
        Index("ix_prospect_docs_prospect_type", "prospect_id", "doc_type"),
        in_check("doc_type", DOC_TYPES, "doc_type"),
        in_check("origin", DOC_ORIGINS, "origin"),
        in_check("status", DOC_STATUSES, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="done", server_default="done")
    error: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)  # summary text (research: unused)
    data_json: Mapped[str | None] = mapped_column(Text)  # research: the brief (CompanyBrief fields); summary: AI fields
    sources_json: Mapped[str | None] = mapped_column(Text)  # [{"kind", "label", "url"}]
    inputs_json: Mapped[str | None] = mapped_column(Text)  # what the AI run used (website, own data, LinkedIn, web)
    based_on_version: Mapped[int | None] = mapped_column(Integer)  # a human edit: the version it started from
    cost_usd_micros: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    web_search_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    company_brief_id: Mapped[int | None] = mapped_column(ForeignKey("company_briefs.id", ondelete="SET NULL"))
    author_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="system", server_default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class ProspectNote(Base):
    __tablename__ = "prospect_notes"
    __table_args__ = (
        Index("ix_prospect_notes_prospect_created", "prospect_id", "created_at"),
        in_check("source", NOTE_SOURCES, "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    author: Mapped[str] = mapped_column(String(80), nullable=False, default="team", server_default="team")
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="app", server_default="app")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)


class WorkerTask(Base):
    __tablename__ = "worker_tasks"
    __table_args__ = (
        Index("ix_worker_tasks_status_created", "status", "created_at"),
        Index("ix_worker_tasks_prospect", "prospect_id"),
        in_check("kind", TASK_KINDS, "kind"),
        in_check("status", TASK_STATUSES, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="queued", server_default="queued")
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    target_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    linkedin_account_id: Mapped[int | None] = mapped_column(ForeignKey("linkedin_accounts.id", ondelete="SET NULL"))
    requested_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id", ondelete="SET NULL"))
    worker_name: Mapped[str | None] = mapped_column(String(80))
    outcome: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(String(500))
    page_loads: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    result_title: Mapped[str | None] = mapped_column(String(300))
    result_url: Mapped[str | None] = mapped_column(String(1000))
    result_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=now_utc)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


__all__ = ["DOC_TYPES", "ProspectDoc", "ProspectNote", "WorkerTask"]

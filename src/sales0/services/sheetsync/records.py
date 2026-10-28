"""Canonical, frozen records produced by the per-tab parsers.

Each record has a natural ``key`` (stable across re-syncs) and an ``ENTITY`` naming the
CRM aggregate it feeds. ``sheet_fields()`` is the dict of SHEET-OWNED fields — the only
values a sync may overwrite. Fields the CRM owns never appear here.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from typing import Any, ClassVar


@dataclass(frozen=True, kw_only=True)
class SheetRecord:
    ENTITY: ClassVar[str] = ""
    key: str

    def sheet_fields(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "key"}

    @property
    def record_id(self) -> tuple[str, str]:
        return (self.ENTITY, self.key)


@dataclass(frozen=True, kw_only=True)
class SheetEnquiry(SheetRecord):
    """Active Enquiries row -> accepted enquiry (+ prospect)."""

    ENTITY: ClassVar[str] = "enquiry"
    key_source: str = "row_hash"  # post | email_summary | row_hash
    date_posted: date | None = None
    date_added: date | None = None
    requirement_summary: str | None = None
    summary_company: str | None = None  # "Company - Topic - City | Mode" split, best effort
    summary_topic: str | None = None
    summary_city: str | None = None
    engagement_mode: str | None = None  # online | offline | hybrid
    emails: tuple[str, ...] = ()
    email_note: str | None = None  # non-empty cell with no valid address in it
    priority: str | None = None  # high | medium | low
    sheet_status: str | None = None  # raw sheet Status, never remapped here
    sheet_notes: str | None = None
    company: str | None = None
    date_email_sent: date | None = None
    date_proposal_sent: date | None = None
    phones: tuple[str, ...] = ()
    contact_link: str | None = None  # a URL typed into the phone column
    post_url: str | None = None  # the Link cell when it is a URL
    link_text: str | None = None  # the Link cell when it is plain text
    full_enquiry: str | None = None
    keyword_used: str | None = None
    sheet_update: str | None = None
    contact_person: str | None = None


@dataclass(frozen=True, kw_only=True)
class SheetDiscarded(SheetEnquiry):
    """Discarded Enquiries row (Status Discarded / No Response / Rejected + note)."""

    ENTITY: ClassVar[str] = "discarded"
    discard_reason: str | None = None  # the sheet's Notes cell


@dataclass(frozen=True, kw_only=True)
class _Person(SheetRecord):
    ENTITY: ClassVar[str] = "prospect"
    key_source: str = "name_company"  # linkedin | name_company
    name: str | None = None
    linkedin_url: str | None = None
    linkedin_slug: str | None = None
    company: str | None = None
    website: str | None = None
    sheet_status: str | None = None
    reason_url: str | None = None  # "Reason of adding" when it is a post link
    reason_text: str | None = None
    extra_link: str | None = None  # the header-less spare column
    sheet_notes: str | None = None
    source_tab: str = ""


@dataclass(frozen=True, kw_only=True)
class SheetOutreachPerson(_Person):
    """Outreach tab row (LinkedIn contact approached)."""


@dataclass(frozen=True, kw_only=True)
class SheetProspect(_Person):
    """Prospects / Working Sheet row. Same ENTITY as outreach: one person, many tabs."""


@dataclass(frozen=True, kw_only=True)
class SheetCompetitor(SheetRecord):
    ENTITY: ClassVar[str] = "competitor"
    website: str | None = None
    website_key: str | None = None
    name: str | None = None
    approx_clients: int | None = None
    client_names: str | None = None
    contact: str | None = None
    emails: tuple[str, ...] = ()
    sheet_notes: str | None = None


@dataclass(frozen=True, kw_only=True)
class SheetGroup(SheetRecord):
    ENTITY: ClassVar[str] = "group"
    name: str | None = None
    platform: str | None = None
    link: str | None = None
    details: str | None = None
    keep_level: str | None = None  # keep | keep-low


@dataclass(frozen=True)
class SheetMetricWeek:
    label: str  # "Week 43"
    week_no: int
    occurrence: int  # 1, or 2+ for a duplicated column header
    raw: str
    value: float | None


@dataclass(frozen=True, kw_only=True)
class SheetMetricRow(SheetRecord):
    ENTITY: ClassVar[str] = "metric"
    metric_set: str = ""  # weekly | secondary
    metric: str = ""
    group: str | None = None  # Secondary: the section label carried down from above
    weekly_target: float | None = None
    weekly_target_raw: str | None = None
    weeks: tuple[SheetMetricWeek, ...] = ()

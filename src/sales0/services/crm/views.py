"""Frozen view models of the CRM (architecture.md -> Phase 2 -> S1 contract).

Routes and templates only ever see these, never ORM objects. Field names are final; the S1
services build them, S2/S4/S5 consume them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class FlagView(_View):
    kind: str
    source: str
    detail: dict[str, Any] | None = None
    created_at: datetime


class FollowUpView(_View):
    id: int
    prospect_id: int | None = None
    lead_id: int | None = None
    who: str
    kind: str
    kind_label: str
    due_on: date
    note: str | None = None
    overdue: bool = False
    auto: bool = False
    done: bool = False
    done_at: datetime | None = None
    channel: str | None = None  # whatsapp | linkedin | email | call (how it WAS done)
    channel_label: str | None = None
    planned_channel: str = "email"  # how it is PLANNED to be done (q16); only email ones are AI-drafted
    planned_channel_label: str = "Email"
    draft_message_id: int | None = None  # the pending follow-up email draft for it, if any (q16)
    draft_state: str | None = None


class DuePanel(_View):
    due_today: list[FollowUpView] = Field(default_factory=list)
    overdue: list[FollowUpView] = Field(default_factory=list)


class TimelineItemView(_View):
    id: int
    kind: str
    kind_label: str
    body: str | None = None
    data: dict[str, Any] | None = None
    actor: str
    occurred_at: datetime
    subject_type: str
    subject_id: int
    href: str | None = None


class TagView(_View):
    id: int
    name: str
    category: str
    count: int = 0


class CharacteristicView(_View):
    """One registry entry (Training provider, Partner, ...); ``count`` = assignments (registry page only)."""

    id: int
    name: str
    system_key: str | None = None
    active: bool = True
    count: int = 0


class ContactEmailView(_View):
    id: int
    email: str
    is_primary: bool = False
    suppressed: bool = False


class ContactPhoneView(_View):
    id: int
    phone: str
    is_primary: bool = False


class ContactView(_View):
    id: int
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    title: str | None = None
    company_id: int | None = None
    company_name: str | None = None
    tags: list[TagView] = Field(default_factory=list)
    origin: str = "manual"


class ContactLinkView(_View):
    entity_type: str
    entity_id: int
    label: str
    href: str | None = None
    role: str | None = None


class ContactDetail(ContactView):
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    email_items: list[ContactEmailView] = Field(default_factory=list)
    phone_items: list[ContactPhoneView] = Field(default_factory=list)
    linkedin_profile_id: int | None = None
    tracked: bool | None = None
    notes: str | None = None
    links: list[ContactLinkView] = Field(default_factory=list)
    timeline: list[TimelineItemView] = Field(default_factory=list)


class DuplicateGroup(_View):
    reason: str
    contacts: list[ContactView] = Field(default_factory=list)


class CompanyView(_View):
    id: int
    name: str
    website: str | None = None
    city: str | None = None
    linkedin_url: str | None = None
    notes: str | None = None


class CompanyRow(CompanyView):
    """A row of the Companies list: the roll-up counts and the union of its entities' characteristics."""

    prospects: int = 0
    clients: int = 0
    contacts: int = 0
    enquiries: int = 0
    open_follow_ups: int = 0
    characteristics: list[CharacteristicView] = Field(default_factory=list)
    priority: str | None = None  # q16: the highest priority among its open prospects


class SheetSourceView(_View):
    """One sheet row feeding a prospect/lead (S2 fills it; fields additive-only)."""

    sheet_tab: str | None = None
    row_number: int | None = None
    sheet_status: str | None = None
    last_seen_at: datetime | None = None


class LinkedEnquiryView(_View):
    id: int
    category: str = "relevant_prospect"  # q16
    requirement_summary: str | None = None
    priority: str | None = None
    review_status: str
    ai_verdict: str | None = None
    date_added: datetime | None = None
    source_url: str | None = None
    company: str | None = None
    sheet_status: str | None = None
    prospect_id: int | None = None
    author_name: str | None = None
    author_profile_url: str | None = None
    link_kind: str | None = None


class LinkedMessageView(_View):
    id: int
    subject: str
    state: str
    sent_at: datetime | None = None
    reply_status: str | None = None
    owner_name: str | None = None


class LinkedReplyView(_View):
    id: int
    message_id: int
    received_at: datetime
    kind: str
    intent: str | None = None
    snippet: str | None = None


class ProspectView(_View):
    id: int
    name: str
    kind: str
    stage: str
    stage_label: str
    status: str
    closed_reason: str | None = None
    source: str
    company_id: int | None = None
    company_name: str | None = None
    contact_id: int | None = None
    contact_name: str | None = None
    linkedin_profile_id: int | None = None
    linkedin_url: str | None = None
    tracked: bool | None = None
    contacted_at: datetime | None = None
    contacted_estimated: bool = False
    responded_at: datetime | None = None
    next_follow_up_on: date | None = None
    flags: list[FlagView] = Field(default_factory=list)
    sheet_status: str | None = None
    sheet_removed_at: datetime | None = None
    sheet_tabs: list[str] = Field(default_factory=list)
    open_lead_id: int | None = None
    created_at: datetime
    characteristics: list[CharacteristicView] = Field(default_factory=list)
    # q16: high | medium | low | None; priority_source 'derived' (from the linked enquiries) | 'manual'
    priority: str | None = None
    priority_source: str = "derived"
    # q16: the non-default enquiry categories among its enquiries (channel_partner, competitor), for the chip
    categories: list[str] = Field(default_factory=list)


class LeadView(_View):
    id: int
    prospect_id: int
    prospect_name: str
    company_name: str | None = None
    stage: str
    stage_label: str
    opened_at: datetime
    closed_at: datetime | None = None
    meeting_at: datetime | None = None
    proposal_sent_on: date | None = None
    value_estimate: int | None = None
    outcome_note: str | None = None
    flags: list[FlagView] = Field(default_factory=list)
    days_since_activity: int | None = None
    company_id: int | None = None
    characteristics: list[CharacteristicView] = Field(default_factory=list)
    priority: str | None = None  # the prospect's (q16)


class ClientView(_View):
    id: int
    name: str
    company_id: int | None = None
    status: str
    source: str
    started_on: date | None = None
    lead_id: int | None = None
    prospect_id: int | None = None
    company_name: str | None = None
    characteristics: list[CharacteristicView] = Field(default_factory=list)
    priority: str | None = None  # the prospect's (q16)


class ProspectDetail(ProspectView):
    notes: str | None = None
    reason_url: str | None = None
    reason_text: str | None = None
    extra_link: str | None = None
    enquiries: list[LinkedEnquiryView] = Field(default_factory=list)
    messages: list[LinkedMessageView] = Field(default_factory=list)
    replies: list[LinkedReplyView] = Field(default_factory=list)
    contacts: list[ContactView] = Field(default_factory=list)
    follow_ups: list[FollowUpView] = Field(default_factory=list)
    timeline: list[TimelineItemView] = Field(default_factory=list)
    lead: LeadView | None = None
    client: ClientView | None = None
    sheet_sources: list[SheetSourceView] = Field(default_factory=list)


class LeadDetail(LeadView):
    timeline: list[TimelineItemView] = Field(default_factory=list)
    follow_ups: list[FollowUpView] = Field(default_factory=list)
    client: ClientView | None = None
    enquiries: list[LinkedEnquiryView] = Field(default_factory=list)
    messages: list[LinkedMessageView] = Field(default_factory=list)


class CompetitorView(_View):
    id: int
    name: str
    website: str | None = None
    approx_clients: int | None = None
    client_names: str | None = None
    contact: str | None = None
    email: str | None = None
    notes: str | None = None
    active: bool = True
    sheet_synced: bool = False
    mention_count: int = 0


class ClientDetail(ClientView):
    notes: str | None = None
    timeline: list[TimelineItemView] = Field(default_factory=list)
    competitors: list[CompetitorView] = Field(default_factory=list)


class CompetitorDetail(CompetitorView):
    """Competitor + its mentions (linked enquiries / clients); S1 fills the shape (additive)."""

    enquiries: list[LinkedEnquiryView] = Field(default_factory=list)
    clients: list[ClientView] = Field(default_factory=list)


class GroupView(_View):
    id: int
    name: str
    platform: str
    link: str | None = None
    details: str | None = None
    keep_level: str | None = None
    active: bool = True
    sheet_synced: bool = False


class CompanyDetail(CompanyView):
    prospects: list[ProspectView] = Field(default_factory=list)
    enquiries: list[LinkedEnquiryView] = Field(default_factory=list)
    contacts: list[ContactView] = Field(default_factory=list)
    timeline: list[TimelineItemView] = Field(default_factory=list)
    leads: list[LeadView] = Field(default_factory=list)
    clients: list[ClientView] = Field(default_factory=list)
    messages: list[LinkedMessageView] = Field(default_factory=list)
    replies: list[LinkedReplyView] = Field(default_factory=list)
    characteristics: list[CharacteristicView] = Field(default_factory=list)
    open_follow_ups: int = 0


class PipelineSummary(_View):
    prospects_by_stage: dict[str, int] = Field(default_factory=dict)
    leads_by_stage: dict[str, int] = Field(default_factory=dict)
    closed_recent: dict[str, int] = Field(default_factory=dict)
    clients_active: int = 0
    clients_legacy: int = 0
    due_today: int = 0
    overdue: int = 0


class BoardColumn(_View):
    stage: str
    label: str
    total: int
    cards: list[ProspectView | LeadView] = Field(default_factory=list)


class Board(_View):
    kind: str
    columns: list[BoardColumn] = Field(default_factory=list)


class ProspectPage(_View):
    items: list[ProspectView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class LeadPage(_View):
    items: list[LeadView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class ClientPage(_View):
    items: list[ClientView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class ContactPage(_View):
    items: list[ContactView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class CompanyPage(_View):
    items: list[CompanyRow] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50

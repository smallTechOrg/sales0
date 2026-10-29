"""Phase 2 enum-like value tuples (CRM). Stored as lowercase strings; see data.md -> Phase 2.

The tuples feed the CHECK constraints of the NEW tables only (never an existing, FK-referenced
table) and the services' validation.
"""

from __future__ import annotations

PROSPECT_KINDS: tuple[str, ...] = ("company_enquiry", "outreach_person")
PROSPECT_STAGES: tuple[str, ...] = ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up")
LEAD_STAGES: tuple[str, ...] = ("meeting_scheduled", "proposal_sent", "won", "lost", "no_response")
STAGE_SOURCES: tuple[str, ...] = ("app", "sheet")
PROSPECT_STATUSES: tuple[str, ...] = ("open", "converted", "closed")
PROSPECT_SOURCES: tuple[str, ...] = ("enquiry", "sheet", "manual", "import")
LEAD_OPENED_BY: tuple[str, ...] = ("reply", "manual", "sheet")
CLIENT_STATUSES: tuple[str, ...] = ("active", "legacy")
CLIENT_SOURCES: tuple[str, ...] = ("lead", "legacy_import", "manual", "sheet")
FOLLOW_UP_KINDS: tuple[str, ...] = ("follow_up_1", "follow_up_2", "final_follow_up", "custom")
# How a contact / follow-up was made (follow_ups.channel and timeline data["channel"]). No CHECK (no table rebuild).
FOLLOW_UP_CHANNELS: tuple[str, ...] = ("whatsapp", "linkedin", "email", "call")
FLAG_ENTITY_TYPES: tuple[str, ...] = ("prospect", "enquiry", "contact", "company", "client", "lead")
FLAG_KINDS: tuple[str, ...] = ("repeat_poster", "competitor_mention", "no_follow_up", "source_row_removed")
FLAG_SOURCES: tuple[str, ...] = ("auto", "user")
TIMELINE_SUBJECTS: tuple[str, ...] = (
    "prospect", "lead", "client", "contact", "company", "enquiry", "competitor", "group",
)
TIMELINE_KINDS: tuple[str, ...] = (
    "created", "stage_change", "note", "email_sent", "reply_received", "lead_created", "lead_stage",
    "client_created", "proposal_sent", "enquiry_linked", "flag_set", "flag_cleared", "follow_up_added",
    "follow_up_done", "follow_up_cancelled", "tracked_changed", "contact_merged", "contact_linked",
    "call", "meeting", "research", "import", "sheet_created", "sheet_updated", "sheet_removed",
    "sheet_restored", "sheet_status",
)
CONTACT_ORIGINS: tuple[str, ...] = ("manual", "enquiry", "sheet", "import", "reply")
TAG_CATEGORIES: tuple[str, ...] = ("role", "segment", "custom")
LINK_ENTITY_TYPES: tuple[str, ...] = ("enquiry", "prospect", "lead", "client", "competitor", "company")
MENTION_ENTITY_TYPES: tuple[str, ...] = ("enquiry", "client", "prospect")
MENTION_SOURCES: tuple[str, ...] = ("auto", "user")
KEEP_LEVELS: tuple[str, ...] = ("keep", "keep_low")
IMPORT_KINDS: tuple[str, ...] = (
    "enquiries", "discarded", "outreach", "prospects", "working", "competitors", "groups", "clients",
    "contacts", "paste",
)
SHEET_ENTITIES: tuple[str, ...] = ("enquiry", "discarded", "prospect", "competitor", "group", "metric")
SHEET_TARGET_TYPES: tuple[str, ...] = ("enquiry", "prospect", "competitor", "channel_group", "sheet_metric")
METRIC_SETS: tuple[str, ...] = ("weekly", "secondary")
CHARACTERISTIC_ENTITY_TYPES: tuple[str, ...] = ("prospect", "client")
ENQUIRY_ORIGINS: tuple[str, ...] = ("discovery", "sheet", "manual", "import")

# q16 (feedback round 2). Enquiry category: what KIND of poster/enquiry it is. ``relevant_prospect`` is a genuine
# enquiry/prospect; the others are filed elsewhere. Assumed: an enum + service validation (no CHECK) so a new value
# needs no table rebuild.
ENQUIRY_CATEGORIES: tuple[str, ...] = ("relevant_prospect", "channel_partner", "competitor")
DEFAULT_ENQUIRY_CATEGORY = "relevant_prospect"
ENQUIRY_CATEGORY_LABELS: dict[str, str] = {
    "relevant_prospect": "Relevant prospect", "channel_partner": "Channel partner", "competitor": "Competitor",
}
# Prospect priority: ``derived`` = the highest priority of its linked enquiries (recomputed); ``manual`` = a person
# set it (never overwritten by an enquiry change or a sheet sync).
PROSPECT_PRIORITY_SOURCES: tuple[str, ...] = ("derived", "manual")

# ---- q17 company model (spec/capabilities/domain-model.md). A COMPANY is the main record; Prospect / Lead / Client are
# lifecycle STAGES of one company. Stored on ``companies`` (plain columns, validated in the service: no CHECK, so no
# table rebuild of the FK-referenced ``companies``).
COMPANY_STAGES: tuple[str, ...] = ("prospect", "lead", "client")
COMPANY_SUB_STATUSES: dict[str, tuple[str, ...]] = {
    "prospect": ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up"),
    "lead": ("meeting_scheduled", "proposal_sent"),
    "client": ("active",),
}
# Off-ramps: company end states a person can reopen (do_not_contact needs an admin). ``lost_client`` = churned client.
OFF_RAMPS: tuple[str, ...] = ("not_interested", "dormant", "do_not_contact", "lost_client")
# Off-ramps only a person sets on the company (the lead/prospect/client rows cannot express them); the projection
# of the ledger rows never overwrites them.
COMPANY_ONLY_OFF_RAMPS: tuple[str, ...] = ("do_not_contact", "lost_client")
# Enquiry deal status (after the inbox review accepted it): the Active Enquiries sheet vocabulary.
ENQUIRY_DEAL_STATUSES: tuple[str, ...] = (
    "open", "contacted", "follow_up_1", "follow_up_2", "final_follow_up", "meeting_scheduled", "proposal_sent",
    "won", "lost", "no_response", "on_hold",
)
PERSON_ROLES: tuple[str, ...] = ("decision_maker", "hr_ld", "influencer", "other")
PERSON_ROLE_LABELS: dict[str, str] = {
    "decision_maker": "Decision maker", "hr_ld": "HR / L&D", "influencer": "Influencer", "other": "Other",
}

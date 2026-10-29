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

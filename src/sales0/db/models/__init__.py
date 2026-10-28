"""ORM models (Alembic autogenerate target): one module per aggregate (see spec/data.md).

Every model is re-exported here; importing this package registers all tables on
``sales0.db.base.metadata``.
"""

from __future__ import annotations

from sales0.db.models import enums, enums_crm
from sales0.db.models.contacts import Contact, ContactEmail, ContactLink, ContactPhone, ContactTag, Tag
from sales0.db.models.crm import Client, Company, Flag, FollowUp, Lead, Prospect
from sales0.db.models._types import UTCDateTime
from sales0.db.models.decisions import TriageDecision
from sales0.db.models.enquiries import Enquiry, EnquiryKeywordHit, EnquirySource
from sales0.db.models.history import StatusHistory
from sales0.db.models.keywords import Keyword
from sales0.db.models.linkedin import LINKEDIN_SESSION_ID, LinkedInSession
from sales0.db.models.llm_calls import LlmCall
from sales0.db.models.meta import AppMeta
from sales0.db.models.offerings import Offering, OfferingGroup
from sales0.db.models.profiles import LinkedinProfile
from sales0.db.models.ops import Digest, ScheduledJob, Suggestion
from sales0.db.models.outreach import CompanyBrief, Deck, MessageTemplate, OutreachEvent, OutreachMessage
from sales0.db.models.replies import OutreachReply, ReplyPollState, Suppression
from sales0.db.models.registries import ChannelGroup, Competitor, CompetitorMention, ImportBatch
from sales0.db.models.runs import ScrapeRun, ScrapeRunKeyword
from sales0.db.models.seen_posts import SeenPost
from sales0.db.models.sheetdata import SheetMetric, SheetMetricWeek, SheetSource
from sales0.db.models.team import GmailConnection, TeamMember
from sales0.db.models.sheet_sync import SheetSyncState
from sales0.db.models.timeline import TimelineEvent
from sales0.db.models.users import User

__all__ = [
    "LINKEDIN_SESSION_ID",
    "ChannelGroup",
    "Client",
    "Company",
    "Competitor",
    "CompetitorMention",
    "Contact",
    "ContactEmail",
    "ContactLink",
    "ContactPhone",
    "ContactTag",
    "Flag",
    "FollowUp",
    "ImportBatch",
    "Lead",
    "Prospect",
    "SheetMetric",
    "SheetMetricWeek",
    "SheetSource",
    "Tag",
    "TimelineEvent",
    "enums_crm",
    "AppMeta",
    "CompanyBrief",
    "Deck",
    "Digest",
    "Enquiry",
    "EnquiryKeywordHit",
    "EnquirySource",
    "GmailConnection",
    "Keyword",
    "LinkedInSession",
    "LinkedinProfile",
    "LlmCall",
    "MessageTemplate",
    "Offering",
    "OfferingGroup",
    "OutreachEvent",
    "OutreachMessage",
    "ScheduledJob",
    "OutreachReply",
    "ReplyPollState",
    "ScrapeRun",
    "ScrapeRunKeyword",
    "SeenPost",
    "SheetSyncState",
    "StatusHistory",
    "Suggestion",
    "Suppression",
    "TeamMember",
    "TriageDecision",
    "UTCDateTime",
    "User",
    "enums",
]

"""ORM models (Alembic autogenerate target): one module per aggregate (see spec/data.md).

Every model is re-exported here; importing this package registers all tables on
``sales0.db.base.metadata``.
"""

from __future__ import annotations

from sales0.db.models import enums
from sales0.db.models._types import UTCDateTime
from sales0.db.models.decisions import TriageDecision
from sales0.db.models.enquiries import Enquiry, EnquiryKeywordHit, EnquirySource
from sales0.db.models.history import StatusHistory
from sales0.db.models.keywords import Keyword
from sales0.db.models.linkedin import LINKEDIN_SESSION_ID, LinkedInSession
from sales0.db.models.llm_calls import LlmCall
from sales0.db.models.meta import AppMeta
from sales0.db.models.offerings import Offering, OfferingGroup
from sales0.db.models.runs import ScrapeRun, ScrapeRunKeyword
from sales0.db.models.sheet_sync import SheetSyncState
from sales0.db.models.users import User

__all__ = [
    "LINKEDIN_SESSION_ID",
    "AppMeta",
    "Enquiry",
    "EnquiryKeywordHit",
    "EnquirySource",
    "Keyword",
    "LinkedInSession",
    "LlmCall",
    "Offering",
    "OfferingGroup",
    "ScrapeRun",
    "ScrapeRunKeyword",
    "SheetSyncState",
    "StatusHistory",
    "TriageDecision",
    "UTCDateTime",
    "User",
    "enums",
]

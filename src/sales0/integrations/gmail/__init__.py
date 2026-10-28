"""Gmail integration (Phase 3D): OAuth, draft/send adapter, safe live-check helpers."""

from sales0.integrations.gmail.client import ApprovedMessage, Draft, FakeGmailClient, GmailClient, RealGmailClient
from sales0.integrations.gmail.errors import GmailError

__all__ = ["ApprovedMessage", "Draft", "FakeGmailClient", "GmailClient", "GmailError", "RealGmailClient"]

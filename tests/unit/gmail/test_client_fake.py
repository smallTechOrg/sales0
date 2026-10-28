from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sales0.integrations.gmail import ApprovedMessage, FakeGmailClient, GmailClient
from sales0.integrations.gmail.errors import ApprovalMismatch, GmailNotFound


def approve(client, draft_id, sha=None):
    return ApprovedMessage(draft_id, sha or client.get_draft(draft_id).sha256, "tester", datetime.now(UTC))


def test_fake_satisfies_protocol():
    assert isinstance(FakeGmailClient(), GmailClient)


def test_send_requires_matching_hash():
    g = FakeGmailClient()
    d = g.create_draft(["x@example.com"], [], "S", "body")
    ids = g.send_draft(approve(g, d))
    assert ids == ("msg-1", "thr-1") and len(g.sent) == 1 and d not in g.raw_drafts


def test_edited_after_approval_is_refused():
    g = FakeGmailClient()
    d = g.create_draft(["x@example.com"], [], "S", "body")
    ok = approve(g, d)
    g.edit_draft_body(d, "body, but changed")
    with pytest.raises(ApprovalMismatch, match="changed after approval"):
        g.send_draft(ok)
    assert g.sent == [] and d in g.raw_drafts


def test_wrong_draft_and_non_approval_refused():
    g = FakeGmailClient()
    d1 = g.create_draft(["x@example.com"], [], "S", "b1")
    d2 = g.create_draft(["x@example.com"], [], "S", "b2")
    with pytest.raises(ApprovalMismatch):
        g.send_draft(approve(g, d1, sha=g.get_draft(d2).sha256))
    with pytest.raises(ApprovalMismatch):
        g.send_draft(ApprovedMessage(d2, g.get_draft(d1).sha256, "t", datetime.now(UTC)))
    with pytest.raises(ApprovalMismatch):
        g.send_draft(d1)  # type: ignore[arg-type]
    assert g.sent == []


def test_delete_and_missing():
    g = FakeGmailClient()
    d = g.create_draft(["x@example.com"], [], "S", "b")
    g.delete_draft(d)
    with pytest.raises(GmailNotFound):
        g.get_draft(d)
    assert [c[0] for c in g.calls][:2] == ["create_draft", "delete_draft"]


def test_list_sent_stub():
    with pytest.raises(NotImplementedError):
        FakeGmailClient().list_sent_since(datetime.now(UTC))

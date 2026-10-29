"""Gmail client: protocol, real (REST via httpx) and in-memory fake.

SAFETY (binding): nothing in this module sends implicitly. ``send_draft`` needs an explicit
``ApprovedMessage`` and re-fetches the draft, recomputes its hash and refuses on mismatch,
so a message edited after approval cannot go out. The outreach service (a later phase) is
the ONLY production caller of ``send_draft``; the ``gmail-send-test`` CLI is the one other,
and it only ever mails the authenticated account itself.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from sales0.integrations.gmail.auth import TokenProvider
from sales0.integrations.gmail.errors import (
    ApprovalMismatch,
    GmailApiError,
    GmailAuthError,
    GmailNotFound,
    GmailScopeError,
    GmailTransientError,
)
from sales0.integrations.gmail.threads import METADATA_HEADERS, ThreadMessage, parse_message
from sales0.integrations.gmail.mime import (
    Attachment,
    DraftContent,
    build_message,
    normalise,
    parse_raw,
)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
UPLOAD_API = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_REAUTH = "run `uv run python -m sales0.cli google-auth` again"


@dataclass(frozen=True)
class ApprovedMessage:
    """A human approval bound to the exact content (``DraftContent.sha256``) of one draft."""

    draft_id: str
    sha256: str
    approved_by: str
    approved_at: datetime


@dataclass(frozen=True)
class Draft:
    draft_id: str
    content: DraftContent

    @property
    def sha256(self) -> str:
        return self.content.sha256


def verify_approval(approved: ApprovedMessage, draft: Draft) -> None:
    if not isinstance(approved, ApprovedMessage):
        raise ApprovalMismatch("send_draft requires an ApprovedMessage; nothing was sent")
    if draft.draft_id != approved.draft_id:
        raise ApprovalMismatch("approval is for a different draft; nothing was sent")
    if draft.sha256 != approved.sha256:
        raise ApprovalMismatch(
            f"draft {draft.draft_id} changed after approval (hash mismatch); nothing was sent — re-approve it"
        )


@runtime_checkable
class GmailClient(Protocol):
    def profile_email(self) -> str: ...

    def create_draft(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
        *,
        thread_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> str: ...

    def get_draft(self, draft_id: str) -> Draft: ...

    def delete_draft(self, draft_id: str) -> None: ...

    def send_draft(self, approved: ApprovedMessage) -> tuple[str, str]: ...

    def get_thread(self, thread_id: str) -> list[ThreadMessage]: ...

    def list_threads_with_new_messages(self, since: datetime, *, limit: int = 100) -> list[str]: ...

    def trash_thread(self, thread_id: str) -> None: ...


def prepare(
    to: list[str],
    cc: list[str],
    subject: str,
    text_body: str,
    html_body: str | None,
    attachments: list[tuple[str, bytes, str]] | None,
) -> DraftContent:
    return normalise(to, cc, subject, text_body, html_body, [Attachment(*a) for a in attachments or []])


# --------------------------------------------------------------------------- real client
@dataclass
class RealGmailClient:
    tokens: TokenProvider
    http: httpx.Client
    max_attempts: int = 4
    sleep: Callable[[float], None] = time.sleep
    _email: str | None = field(default=None, init=False)

    # -- transport -----------------------------------------------------------------
    def _request(self, method: str, url: str, *, retry_5xx: bool = True, **kwargs: Any) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self.tokens.access_token()}"}
            try:
                r = self.http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                # A send may or may not have happened; never retry it blindly.
                if retry_5xx and attempt < self.max_attempts:
                    self.sleep(self._backoff(attempt, None))
                    continue
                raise GmailTransientError(f"Gmail unreachable ({exc.__class__.__name__})") from exc
            kwargs["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
            if r.status_code < 300:
                return r
            if r.status_code == 401:
                if not refreshed:
                    refreshed = True
                    self.tokens.access_token(force=True)
                    attempt -= 1
                    continue
                raise GmailAuthError(f"Google rejected the access token: {_REAUTH}.")
            reason = self._reason(r)
            if r.status_code == 403 and ("insufficient" in reason.lower() or "scope" in reason.lower()):
                raise GmailScopeError(f"The Google token does not allow this action ({reason}): {_REAUTH}.")
            rate = r.status_code == 429 or (r.status_code == 403 and "ratelimit" in reason.lower().replace(" ", ""))
            retryable = rate or (r.status_code >= 500 and retry_5xx)
            if retryable and attempt < self.max_attempts:
                self.sleep(self._backoff(attempt, r.headers.get("Retry-After")))
                continue
            if r.status_code == 404:
                raise GmailNotFound(f"Gmail says not found ({reason or url})")
            if retryable or r.status_code >= 500:
                raise GmailTransientError(f"Gmail kept failing with HTTP {r.status_code} ({reason})")
            if r.status_code == 403:
                raise GmailScopeError(f"Gmail refused the request ({reason}): check the Gmail API is enabled; {_REAUTH}.")
            raise GmailApiError(f"Gmail HTTP {r.status_code}: {reason}")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 60.0)
        return min(2.0 ** (attempt - 1), 30.0)

    @staticmethod
    def _reason(r: httpx.Response) -> str:
        try:
            err = r.json().get("error", {})
            if isinstance(err, dict):
                reasons = ",".join(e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict))
                return f"{err.get('message', '')} {reasons}".strip()
            return str(err)
        except (ValueError, AttributeError):
            return ""

    # -- API -------------------------------------------------------------------------
    def profile_email(self) -> str:
        if self._email:
            return self._email
        if self.tokens.token.email:
            self._email = self.tokens.token.email
            return self._email
        email = self._request("GET", USERINFO).json().get("email")
        if not email:
            raise GmailScopeError(f"Google returned no email address for this token: {_REAUTH}.")
        self._email = str(email)
        return self._email

    def create_draft(self, to, cc, subject, text_body, html_body=None, attachments=None, *, thread_id=None, headers=None) -> str:  # type: ignore[no-untyped-def]
        self.tokens.require_scopes("drafts")
        raw = build_message(prepare(to, cc, subject, text_body, html_body, attachments), headers)
        if thread_id:
            # a follow-up joins the original Gmail thread: JSON body with threadId (no attachments on follow-ups)
            r = self._request(
                "POST", f"{API}/drafts",
                json={"message": {"raw": base64.urlsafe_b64encode(raw).decode("ascii"), "threadId": thread_id}},
            )
            return str(r.json()["id"])
        r = self._request(
            "POST",
            f"{UPLOAD_API}/drafts",
            params={"uploadType": "media"},
            content=raw,
            headers={"Content-Type": "message/rfc822"},
        )
        return str(r.json()["id"])

    def get_draft(self, draft_id: str) -> Draft:
        self.tokens.require_scopes("drafts")
        r = self._request("GET", f"{API}/drafts/{draft_id}", params={"format": "raw"})
        raw = base64.urlsafe_b64decode(r.json()["message"]["raw"] + "===")
        return Draft(draft_id, parse_raw(raw))

    def delete_draft(self, draft_id: str) -> None:
        self.tokens.require_scopes("drafts")
        self._request("DELETE", f"{API}/drafts/{draft_id}")

    def send_draft(self, approved: ApprovedMessage) -> tuple[str, str]:
        if not isinstance(approved, ApprovedMessage):
            raise ApprovalMismatch("send_draft requires an ApprovedMessage; nothing was sent")
        self.tokens.require_scopes("drafts")
        verify_approval(approved, self.get_draft(approved.draft_id))
        # No 5xx retry: a send whose answer was lost must not be repeated blindly.
        r = self._request("POST", f"{API}/drafts/send", json={"id": approved.draft_id}, retry_5xx=False)
        body = r.json()
        return str(body["id"]), str(body["threadId"])

    def trash_thread(self, thread_id: str) -> None:
        """Move a whole thread to Trash (recoverable for 30 days; never permanent delete)."""
        self.tokens.require_scopes("trash")
        self._request("POST", f"{API}/threads/{thread_id}/trash")

    # -- reply tracking (READ-ONLY: gmail.modify covers read; nothing here mutates the mailbox) --
    def get_thread(self, thread_id: str) -> list[ThreadMessage]:
        """Every message of a thread, oldest first: headers + snippet + labels only (no body)."""
        self.tokens.require_scopes("read")
        r = self._request(
            "GET",
            f"{API}/threads/{thread_id}",
            params=[("format", "metadata"), *[("metadataHeaders", h) for h in METADATA_HEADERS]],
        )
        msgs = [parse_message({**m, "threadId": m.get("threadId", thread_id)}) for m in r.json().get("messages") or []]
        return sorted(msgs, key=lambda m: m.date)

    def list_threads_with_new_messages(self, since: datetime, *, limit: int = 100) -> list[str]:
        """Ids of threads that received a message after ``since`` (any label except spam/trash)."""
        self.tokens.require_scopes("read")
        ids: list[str] = []
        page: str | None = None
        while len(ids) < limit:
            params: dict[str, Any] = {"q": f"after:{int(since.timestamp())}", "maxResults": min(100, limit - len(ids))}
            if page:
                params["pageToken"] = page
            body = self._request("GET", f"{API}/threads", params=params).json()
            ids += [str(t["id"]) for t in body.get("threads") or []]
            page = body.get("nextPageToken")
            if not page:
                break
        return ids[:limit]


# --------------------------------------------------------------------------- fake client
class FakeGmailClient:
    """In-memory Gmail for every other test and service. Records calls in ``calls``."""

    def __init__(self, email: str = "me@example.com") -> None:
        self.email = email
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raw_drafts: dict[str, bytes] = {}
        self.sent: list[dict[str, Any]] = []
        self.threads: dict[str, list[ThreadMessage]] = {}
        self._n = 0

    def profile_email(self) -> str:
        self.calls.append(("profile_email", ()))
        return self.email

    def create_draft(self, to, cc, subject, text_body, html_body=None, attachments=None, *, thread_id=None, headers=None) -> str:  # type: ignore[no-untyped-def]
        self.calls.append(("create_draft", (tuple(to), tuple(cc), subject)))
        raw = build_message(prepare(to, cc, subject, text_body, html_body, attachments), headers)
        self._n += 1
        draft_id = f"draft-{self._n}"
        self.raw_drafts[draft_id] = raw
        if thread_id:
            self.draft_threads = getattr(self, "draft_threads", {})
            self.draft_threads[draft_id] = thread_id
        return draft_id

    def get_draft(self, draft_id: str) -> Draft:
        self.calls.append(("get_draft", (draft_id,)))
        if draft_id not in self.raw_drafts:
            raise GmailNotFound(f"draft {draft_id} not found")
        return Draft(draft_id, parse_raw(self.raw_drafts[draft_id]))

    def delete_draft(self, draft_id: str) -> None:
        self.calls.append(("delete_draft", (draft_id,)))
        if self.raw_drafts.pop(draft_id, None) is None:
            raise GmailNotFound(f"draft {draft_id} not found")

    def send_draft(self, approved: ApprovedMessage) -> tuple[str, str]:
        if not isinstance(approved, ApprovedMessage):
            raise ApprovalMismatch("send_draft requires an ApprovedMessage; nothing was sent")
        self.calls.append(("send_draft", (approved.draft_id,)))
        verify_approval(approved, self.get_draft(approved.draft_id))
        raw = self.raw_drafts.pop(approved.draft_id)
        n = len(self.sent) + 1
        thread = getattr(self, "draft_threads", {}).pop(approved.draft_id, None) or f"thr-{n}"
        self.sent.append({"message_id": f"msg-{n}", "thread_id": thread, "raw": raw, "approved": approved})
        from datetime import UTC

        c = parse_raw(raw)
        self.threads.setdefault(thread, []).append(
            ThreadMessage(
                id=f"msg-{n}", thread_id=thread, from_addr=self.email.lower(), from_name="",
                to_addrs=tuple(a.lower() for a in c.to), cc_addrs=tuple(a.lower() for a in c.cc), subject=c.subject,
                date=datetime.now(UTC), snippet=c.text_body[:100], labels=("SENT",), message_id_header=f"<msg-{n}@fake.example>",
            )
        )
        return f"msg-{n}", thread

    def trash_thread(self, thread_id: str) -> None:
        self.calls.append(("trash_thread", (thread_id,)))
        self.trashed = getattr(self, "trashed", []) + [thread_id]

    def get_thread(self, thread_id: str) -> list[ThreadMessage]:
        self.calls.append(("get_thread", (thread_id,)))
        if thread_id not in self.threads:
            raise GmailNotFound(f"thread {thread_id} not found")
        return sorted(self.threads[thread_id], key=lambda m: m.date)

    def list_threads_with_new_messages(self, since: datetime, *, limit: int = 100) -> list[str]:
        self.calls.append(("list_threads_with_new_messages", (since,)))
        found = [t for t, ms in self.threads.items() if any(m.date > since for m in ms)]
        return found[:limit]

    def add_reply(
        self, thread_id: str, from_addr: str, *, snippet: str = "Thanks, interested.", subject: str = "Re: hello",
        date: datetime | None = None, labels: tuple[str, ...] = ("INBOX", "UNREAD"), **headers: str,
    ) -> ThreadMessage:
        """Test helper: an inbound message appears in ``thread_id``. ``headers`` are the lowercase
        ThreadMessage fields (auto_submitted, precedence, content_type, ...)."""
        from datetime import UTC

        self._n += 1
        known = {"auto_submitted", "x_autoreply", "precedence", "content_type", "x_failed_recipients", "in_reply_to", "references", "return_path"}
        extra = {k: v for k, v in headers.items() if k in known}
        msg = ThreadMessage(
            id=f"in-{self._n}", thread_id=thread_id, from_addr=from_addr.lower(), from_name="", to_addrs=(self.email,),
            cc_addrs=(), subject=subject, date=date or datetime.now(UTC), snippet=snippet, labels=labels, **extra,
        )  # fmt: skip
        self.threads.setdefault(thread_id, []).append(msg)
        return msg

    # test helper: simulate someone editing the draft in Gmail after approval
    def edit_draft_body(self, draft_id: str, text_body: str) -> None:
        c = parse_raw(self.raw_drafts[draft_id])
        self.raw_drafts[draft_id] = build_message(
            DraftContent(c.to, c.cc, c.subject, text_body, c.html_body, c.attachments)
        )

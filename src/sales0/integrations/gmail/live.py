"""Operator commands: ``google-auth``, ``gmail-check``, ``gmail-send-test``.

Not used by the test suite against real Google (tests inject ``FakeGmailClient``).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import httpx

from sales0.config import Settings
from sales0.integrations.gmail.auth import TokenProvider, load_token, run_consent_flow
from sales0.integrations.gmail.client import ApprovedMessage, GmailClient, RealGmailClient
from sales0.integrations.gmail.errors import GmailError

# A tiny but valid one-page PDF, so the attachment path is exercised for real.
_TINY_PDF = (
    b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)


def build_real_client(settings: Settings) -> RealGmailClient:
    token = load_token(settings.google_token_file)
    http = httpx.Client(timeout=30)
    return RealGmailClient(TokenProvider(token, http), http)


def _err(exc: GmailError) -> int:
    print(f"gmail ERROR type={exc.__class__.__name__} {exc}", file=sys.stderr)
    return 1


def google_auth(settings: Settings, *, consent=run_consent_flow) -> int:  # type: ignore[no-untyped-def]
    try:
        token = consent(settings.google_client_secret_file, settings.google_token_file)
    except GmailError as exc:
        return _err(exc)
    print(f"google-auth OK email={token.email or 'unknown'} token={settings.google_token_file}")
    return 0


def gmail_check(client: GmailClient) -> int:
    """Draft to self with a PDF, read it back, delete it. NEVER sends."""
    try:
        me = client.profile_email()
        draft_id = client.create_draft(
            [me], [], "sales0 gmail-check (draft, never sent)", "Draft created by sales0 gmail-check.\n",
            "<p>Draft created by <b>sales0 gmail-check</b>.</p>", [("check.pdf", _TINY_PDF, "application/pdf")],
        )
        try:
            draft = client.get_draft(draft_id)
            ok = (
                draft.content.to == (me,)
                and len(draft.content.attachments) == 1
                and draft.content.attachments[0].data == _TINY_PDF
            )
        finally:
            client.delete_draft(draft_id)
    except GmailError as exc:
        return _err(exc)
    if not ok:
        print("gmail ERROR type=roundtrip the fetched draft did not match what was created", file=sys.stderr)
        return 1
    print(f"gmail OK address={me} draft created, fetched with attachment, deleted; nothing was sent")
    return 0


def gmail_send_test(
    client: GmailClient, *, yes_send: bool, to: str | None = None, allow_external: bool = False
) -> int:
    """Send ONE labelled test email, only ever to the authenticated account itself."""
    if not yes_send:
        print("gmail ERROR refusing to send without --yes-send", file=sys.stderr)
        return 2
    try:
        me = client.profile_email()
        recipient = to or me
        if recipient.strip().lower() != me.lower() and not allow_external:
            print(
                f"gmail ERROR refusing to send to {recipient}: gmail-send-test only mails your own address ({me})",
                file=sys.stderr,
            )
            return 2
        draft_id = client.create_draft(
            [recipient], [], "[sales0 TEST] Gmail send check — please ignore",
            "This is a test email sent by `sales0 gmail-send-test`. It was sent to your own address. "
            "No action needed.\n",
        )
        draft = client.get_draft(draft_id)
        approved = ApprovedMessage(draft_id, draft.sha256, "gmail-send-test --yes-send", datetime.now(UTC))
        message_id, thread_id = client.send_draft(approved)
    except GmailError as exc:
        return _err(exc)
    print(f"gmail OK sent test email to={recipient} message_id={message_id} thread_id={thread_id}")
    try:  # clean up the test mail (Sent + Inbox copies share the thread): move it to Trash
        client.trash_thread(thread_id)
        print(f"gmail OK test thread {thread_id} moved to Trash")
    except GmailError as exc:
        print(f"gmail WARN could not trash the test thread ({exc}); delete it by hand", file=sys.stderr)
    return 0


def _settings_or_exit() -> Settings | None:
    from pydantic import ValidationError

    from sales0.config import get_settings

    try:
        return get_settings()
    except ValidationError as exc:
        print(f"gmail ERROR invalid configuration: {exc.error_count()} error(s) — see .env.example", file=sys.stderr)
        return None


def run_command(args) -> int:  # type: ignore[no-untyped-def]
    settings = _settings_or_exit()
    if settings is None:
        return 1
    if args.command == "google-auth":
        return google_auth(settings)
    try:
        client = build_real_client(settings)
    except GmailError as exc:
        return _err(exc)
    if args.command == "gmail-check":
        return gmail_check(client)
    if args.to and not args.allow_external:
        print("gmail ERROR --to requires --allow-external (dangerous)", file=sys.stderr)
        return 2
    return gmail_send_test(client, yes_send=args.yes_send, to=args.to, allow_external=args.allow_external)


__all__ = ["build_real_client", "gmail_check", "gmail_send_test", "google_auth", "run_command"]

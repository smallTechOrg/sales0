"""Per-member Gmail connections: encrypted token storage, status, client factory, disconnect.

Public API for the outreach services:

* ``client_for_member(member_id, *, http=None) -> GmailClient``
* ``verify_client_identity(member_id, client) -> str``  (the address mail will really go from)
* ``record_failure(member_id, exc)``  (flip status on GmailAuthError / GmailScopeError)
* ``connection_status(db, member_id) -> ConnectionView``

Tests inject a fake with ``set_client_factory(lambda member_id, http: FakeGmailClient(...))``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from sales0.clock import utcnow
from sales0.config import Settings, get_settings
from sales0.db.engine import session_scope
from sales0.db.models import GmailConnection, TeamMember
from sales0.errors import DomainError
from sales0.integrations.gmail.auth import StoredToken, TokenProvider
from sales0.integrations.gmail.client import GmailClient, RealGmailClient
from sales0.integrations.gmail.errors import GmailAuthError, GmailError, GmailNotConfigured, GmailScopeError
from sales0.logging import get_logger
from sales0.services.team import crypto
from sales0.services.team.members import get_member, normalise_email

log = get_logger("sales0.team.connections")

NOT_CONNECTED = "not_connected"
STATUS_LABELS = {
    "connected": "Connected",
    "needs_reauth": "Needs reconnect",
    "scope_insufficient": "Missing permission",
    "disconnected": "Disconnected",
    NOT_CONNECTED: "Not connected",
}

ClientFactory = Callable[[int, "httpx.Client | None"], GmailClient]
_factory: ClientFactory | None = None


def set_client_factory(factory: ClientFactory | None) -> None:
    """Install (or clear with None) the factory ``client_for_member`` uses. Test seam."""
    global _factory
    _factory = factory


def reset_client_factory() -> None:
    set_client_factory(None)


@dataclass(frozen=True)
class ConnectionView:
    member_id: int
    status: str  # connected | needs_reauth | scope_insufficient | disconnected | not_connected
    label: str
    email: str | None
    connected_at: datetime | None
    last_error: str | None

    @property
    def usable(self) -> bool:
        return self.status == "connected"


def _clip_error(message: str) -> str:
    return message if len(message) <= 300 else message[:299] + "…"


def _row(db: Session, member_id: int) -> GmailConnection | None:
    from sqlalchemy import select

    return db.execute(select(GmailConnection).where(GmailConnection.member_id == member_id)).scalar_one_or_none()


def scope_status(scopes: list[str]) -> str:
    """'connected' when the token can draft, send and trash; else 'scope_insufficient'."""
    probe = TokenProvider(StoredToken("", "", "", list(scopes)), None)  # type: ignore[arg-type]  # scope check only
    try:
        probe.require_scopes("drafts")
        probe.require_scopes("trash")
    except GmailScopeError:
        return "scope_insufficient"
    return "connected"


def connection_status(db: Session, member_id: int) -> ConnectionView:
    row = _row(db, member_id)
    if row is None:
        return ConnectionView(member_id, NOT_CONNECTED, STATUS_LABELS[NOT_CONNECTED], None, None, None)
    return ConnectionView(
        member_id, row.status, STATUS_LABELS.get(row.status, row.status), row.email, row.connected_at, row.last_error
    )


def note_mismatch(db: Session, member: TeamMember, got_email: str) -> DomainError:
    """Record (without storing any token) that the wrong Google account was used."""
    message = (
        f"That Google account is {got_email}, but {member.name}'s team address is {member.email}. "
        "Nothing was connected - sign in with the matching Google account (use the account picker)."
    )
    row = _row(db, member.id)
    if row is None:
        db.add(GmailConnection(member_id=member.id, email=member.email, status="disconnected",
                               last_error=_clip_error(message)))
    else:
        row.last_error = _clip_error(message)
    db.flush()
    return DomainError("gmail_email_mismatch", message, 400)


def store_token(db: Session, member_id: int, token: StoredToken, *, settings: Settings | None = None) -> ConnectionView:
    """Encrypt and save ``token`` for the member. Refuses (DomainError) unless the Google
    account's email equals the member's email (lowercased); the token is then NOT stored."""
    member = get_member(db, member_id)
    got = normalise_email(token.email or "")
    if not got:
        raise DomainError(
            "gmail_email_unknown",
            "Google did not tell us which account signed in, so nothing was connected. Try again.",
            400,
        )
    if got != normalise_email(member.email):
        raise note_mismatch(db, member, got)
    if not token.refresh_token:
        raise DomainError("gmail_no_refresh_token", "Google returned no refresh token - try connecting again.", 400)
    status = scope_status(token.scopes)
    now = utcnow()
    row = _row(db, member_id)
    if row is None:
        row = GmailConnection(member_id=member_id, email=got)
        db.add(row)
    row.email = got
    row.scopes = sorted(token.scopes)
    row.token_encrypted = crypto.encrypt(json.dumps(asdict(token)), settings)
    row.status = status
    row.connected_at = now
    row.last_error = (
        None if status == "connected"
        else "Google did not grant the Gmail permission sales0 needs (compose/send). Reconnect and tick every box."
    )
    db.flush()
    log.info("gmail_connection_stored", member_id=member_id, status=status)
    return connection_status(db, member_id)


def load_token(db: Session, member_id: int, *, settings: Settings | None = None) -> StoredToken:
    row = _row(db, member_id)
    if row is None or not row.token_encrypted or row.status == "disconnected":
        raise GmailNotConfigured("Gmail is not connected for this team member: open Team and press Connect my Gmail.")
    try:
        data = json.loads(crypto.decrypt(row.token_encrypted, settings))
    except crypto.DecryptionError as exc:
        raise GmailAuthError(f"{exc} Reconnect Gmail on the Team page.") from exc
    return StoredToken(**data)


def _set_status(member_id: int, status: str, error: str | None) -> None:
    with session_scope() as db:
        row = _row(db, member_id)
        if row is not None:
            row.status = status
            row.last_error = _clip_error(error) if error else None


def record_failure(member_id: int, exc: Exception) -> None:
    """Flip the stored status when Google rejected the credentials (scope first: it subclasses auth)."""
    if isinstance(exc, GmailScopeError):
        _set_status(member_id, "scope_insufficient", str(exc))
    elif isinstance(exc, GmailAuthError):
        _set_status(member_id, "needs_reauth", str(exc))


def client_for_member(member_id: int, *, http: httpx.Client | None = None) -> GmailClient:
    """A Gmail client acting as this member. Raises GmailNotConfigured / GmailAuthError when the
    member has no usable connection. Tests inject a fake through ``set_client_factory``."""
    if _factory is not None:
        return _factory(member_id, http)
    with session_scope() as db:
        member = get_member(db, member_id)
        view = connection_status(db, member_id)
        if view.status == "needs_reauth":
            raise GmailAuthError("Gmail needs to be reconnected for this member: open Team and press Connect my Gmail.")
        if view.status == "scope_insufficient":
            raise GmailScopeError("Gmail is missing a permission for this member: reconnect Gmail on the Team page.")
        token = load_token(db, member_id)
        if normalise_email(token.email or "") != normalise_email(member.email):
            raise GmailAuthError("The connected Google account does not match this member's email: reconnect Gmail.")
    client = http or httpx.Client(timeout=30)
    return RealGmailClient(TokenProvider(token, client), client)


def verify_client_identity(member_id: int, client: GmailClient) -> str:
    """Ask Gmail who the client really is and require it to equal the member's email.
    Returns the address; raises GmailAuthError (and flags needs_reauth) on a mismatch."""
    try:
        address = normalise_email(client.profile_email())
    except GmailError as exc:
        record_failure(member_id, exc)
        raise
    with session_scope() as db:
        expected = normalise_email(get_member(db, member_id).email)
    if address != expected:
        err = GmailAuthError(
            f"Gmail is connected as {address} but this member's address is {expected}: reconnect Gmail on the Team page."
        )
        record_failure(member_id, err)
        raise err
    return address


def disconnect(
    db: Session, member_id: int, *, http: httpx.Client | None = None, settings: Settings | None = None
) -> ConnectionView:
    """Best-effort revoke at Google, then mark disconnected and wipe the stored token."""
    settings = settings or get_settings()
    row = _row(db, member_id)
    if row is None:
        return connection_status(db, member_id)
    revoked = False
    if row.token_encrypted:
        try:
            token = StoredToken(**json.loads(crypto.decrypt(row.token_encrypted, settings)))
        except (crypto.DecryptionError, TypeError, ValueError):
            token = None
        if token is not None:
            client = http or httpx.Client(timeout=15)
            try:
                resp = client.post(settings.google_revoke_endpoint, data={"token": token.refresh_token})
                revoked = resp.status_code == 200
            except httpx.HTTPError as exc:
                log.warning("gmail_revoke_failed", member_id=member_id, error_type=type(exc).__name__)
            finally:
                if http is None:
                    client.close()
    row.token_encrypted = None
    row.status = "disconnected"
    row.scopes = None
    row.last_error = None if revoked else "Google could not be told to revoke access; remove sales0 at myaccount.google.com/permissions."
    db.flush()
    log.info("gmail_disconnected", member_id=member_id, revoked=revoked)
    return connection_status(db, member_id)

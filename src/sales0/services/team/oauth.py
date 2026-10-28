"""Google OAuth (web server flow with PKCE) for connecting a member's Gmail.

The client JSON is read from ``SALES0_GOOGLE_CLIENT_SECRET_PATH`` and may be either type:

* Desktop app (``installed`` block): Google accepts ``http://localhost`` / ``http://127.0.0.1`` on
  ANY port, so locally the redirect URI is the origin the user opened sales0 on plus
  ``/oauth/google/callback``. Loopback only: a hosted deployment needs a Web client.
* Web application (``web`` block): Google only accepts redirect URIs registered in the Cloud
  console, exactly. With ``SALES0_PUBLIC_BASE_URL`` set the URI is that URL plus the callback
  path; it is checked against the JSON's ``redirect_uris`` (when present) before Google is called.

Non-loopback hosts are refused unless ``SALES0_PUBLIC_BASE_URL`` is set.

Pending authorisations (state -> PKCE verifier, bound member, expiry) live in this process's
memory: single use, 10 minutes. A server restart mid-consent just means "try again".
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx

from sales0.clock import utcnow
from sales0.config import LOOPBACK_HOSTS, Settings, get_settings
from sales0.errors import DomainError
from sales0.integrations.gmail.auth import (
    SCOPES,
    StoredToken,
    client_kind,
    client_redirect_uris,
    read_client_secret,
)
from sales0.services.team import crypto

CALLBACK_PATH = "/oauth/google/callback"
STATE_TTL = timedelta(minutes=10)
_MAX_PENDING = 200


# ------------------------------------------------------------------ PKCE
def new_code_verifier() -> str:
    return crypto.random_token(48)  # 64 url-safe chars, within RFC 7636's 43-128


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ------------------------------------------------------------------ state store
@dataclass(frozen=True)
class PendingAuth:
    state: str
    verifier: str
    member_id: int
    redirect_uri: str
    expires_at: datetime


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, PendingAuth] = {}

    def create(self, member_id: int, redirect_uri: str, *, now: datetime | None = None) -> PendingAuth:
        now = now or utcnow()
        pending = PendingAuth(crypto.random_token(32), new_code_verifier(), member_id, redirect_uri, now + STATE_TTL)
        with self._lock:
            for key in [k for k, v in self._items.items() if v.expires_at <= now]:
                del self._items[key]
            while len(self._items) >= _MAX_PENDING:
                del self._items[next(iter(self._items))]
            self._items[pending.state] = pending
        return pending

    def consume(self, state: str | None, member_id: int | None, *, now: datetime | None = None) -> PendingAuth:
        """Single use: the state is removed even when it is then rejected."""
        now = now or utcnow()
        with self._lock:
            pending = self._items.pop(state, None) if state else None
        if pending is None:
            raise DomainError(
                "oauth_state_invalid",
                "That Google sign-in link is unknown or was already used. Press Connect my Gmail to start again.",
                400,
            )
        if pending.expires_at <= now:
            raise DomainError(
                "oauth_state_expired", "That Google sign-in took too long (over 10 minutes). Start again.", 400
            )
        if member_id != pending.member_id:
            raise DomainError(
                "oauth_state_member",
                "That Google sign-in was started for a different team member. Switch to them and start again.",
                403,
            )
        return pending

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


STORE = StateStore()


# ------------------------------------------------------------------ redirect uri / auth url
def redirect_uri_for(scheme: str, host: str, port: int | None, settings: Settings | None = None) -> str:
    """The callback URL for the origin the user is on. ``host`` has no port or brackets."""
    settings = settings or get_settings()
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/") + CALLBACK_PATH
    bare = host.strip("[]").lower()
    if bare not in LOOPBACK_HOSTS:
        raise DomainError(
            "oauth_host_not_allowed",
            f"Google sign-in only works when sales0 is opened on localhost or 127.0.0.1 (you are on {bare}). "
            "Open http://127.0.0.1:<port>/team, or set SALES0_PUBLIC_BASE_URL for a hosted deployment.",
            400,
        )
    shown = f"[{bare}]" if ":" in bare else bare
    default = 443 if scheme == "https" else 80
    netloc = shown if port in (None, default) else f"{shown}:{port}"
    return f"{scheme}://{netloc}{CALLBACK_PATH}"


def redirect_uri_for_request(url: str, host_header: str, settings: Settings | None = None) -> str:
    parts = urlsplit(f"{urlsplit(url).scheme}://{host_header}")
    return redirect_uri_for(parts.scheme, parts.hostname or "", parts.port, settings)


def check_redirect_for_client(redirect_uri: str, settings: Settings | None = None) -> None:
    """Fail early, with the exact URI, when Google would refuse the redirect for this client JSON."""
    settings = settings or get_settings()
    path = settings.google_client_secret_file
    kind = client_kind(path)
    host = (urlsplit(redirect_uri).hostname or "").lower()
    if kind == "installed" and host not in LOOPBACK_HOSTS:
        raise DomainError(
            "oauth_client_type",
            f"The Google client in {path.name} is a Desktop app client, which only allows loopback "
            f"redirects, but the redirect would be {redirect_uri}. Create an OAuth client of type "
            "'Web application' with that exact Authorized redirect URI and use its JSON instead.",
            400,
        )
    listed = client_redirect_uris(path)
    if kind == "web" and listed and redirect_uri not in listed:
        raise DomainError(
            "oauth_redirect_not_registered",
            f"The redirect URI {redirect_uri} is not listed in the Google client JSON "
            f"(it lists: {', '.join(listed)}). Add exactly {redirect_uri} under Authorized redirect URIs "
            "in Google Cloud Console and download the JSON again (or fix SALES0_PUBLIC_BASE_URL).",
            400,
        )


def connect_hint(settings: Settings | None = None) -> dict[str, str] | None:
    """What the Team page tells the admin about the Google client; None when no client JSON yet."""
    settings = settings or get_settings()
    try:
        kind = client_kind(settings.google_client_secret_file)
    except Exception:  # noqa: BLE001 - a missing/bad file is reported by the Connect button itself
        return None
    if settings.public_base_url:
        uri = settings.public_base_url.rstrip("/") + CALLBACK_PATH
        text = f"Register this exact Authorized redirect URI on a Web application client: {uri}"
        if kind == "installed":
            text += " (the current client JSON is a Desktop app client, which Google only accepts on loopback)."
        return {"kind": kind, "redirect_uri": uri, "text": text}
    return {
        "kind": kind,
        "redirect_uri": "",
        "text": "Local use: Google accepts this loopback address on any port for a Desktop app client. "
        "For a hosted deployment set SALES0_PUBLIC_BASE_URL and use a Web application client.",
    }


def build_auth_url(
    pending: PendingAuth, login_hint: str, *, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    client = read_client_secret(settings.google_client_secret_file)
    check_redirect_for_client(pending.redirect_uri, settings)
    query = {
        "client_id": client["client_id"],
        "redirect_uri": pending.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "login_hint": login_hint,
        "state": pending.state,
        "code_challenge": code_challenge(pending.verifier),
        "code_challenge_method": "S256",
    }
    return f"{settings.google_auth_endpoint}?{urlencode(query)}"


# ------------------------------------------------------------------ code exchange
def _id_token_email(id_token: str) -> str | None:
    """Payload of an id_token received directly from the token endpoint over TLS (no signature check needed)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except (IndexError, ValueError):
        return None


def exchange_code(
    code: str, pending: PendingAuth, http: httpx.Client, *, settings: Settings | None = None
) -> StoredToken:
    """Trade the authorisation code for tokens and resolve the signed-in email."""
    settings = settings or get_settings()
    client = read_client_secret(settings.google_client_secret_file)
    try:
        resp = http.post(
            settings.google_token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": pending.redirect_uri,
                "code_verifier": pending.verifier,
            },
        )
    except httpx.HTTPError as exc:
        raise DomainError("oauth_unreachable", "Could not reach Google to finish sign-in. Try again.", 502) from exc
    if resp.status_code != 200:
        raise DomainError(
            "oauth_exchange_failed",
            "Google rejected the sign-in (the code may have expired). Press Connect my Gmail to try again.",
            400,
        )
    body = resp.json()
    refresh = body.get("refresh_token")
    if not refresh:
        raise DomainError(
            "oauth_no_refresh_token",
            "Google returned no refresh token. Remove sales0 at https://myaccount.google.com/permissions "
            "and connect again.",
            400,
        )
    scopes = sorted(str(body.get("scope", "")).split())
    email: str | None = None
    access = body.get("access_token")
    if access:
        try:
            info = http.get(settings.google_userinfo_endpoint, headers={"Authorization": f"Bearer {access}"})
            if info.status_code == 200:
                email = info.json().get("email")
        except (httpx.HTTPError, ValueError):
            email = None
    if not email and body.get("id_token"):
        email = _id_token_email(str(body["id_token"]))
    return StoredToken(client["client_id"], client["client_secret"], refresh, scopes, email)


__all__ = [
    "CALLBACK_PATH",
    "STORE",
    "PendingAuth",
    "StateStore",
    "build_auth_url",
    "check_redirect_for_client",
    "connect_hint",
    "code_challenge",
    "exchange_code",
    "redirect_uri_for",
    "redirect_uri_for_request",
]


def reset_state_store() -> None:
    """Test seam: forget every pending authorisation."""
    STORE.clear()

"""OAuth for the installed-app (Desktop client) flow, token storage and refresh.

The consent step uses ``google-auth-oauthlib`` (loopback redirect on a free port); token
refresh is a plain POST via ``httpx`` so it is testable with ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from sales0.integrations.gmail.errors import GmailAuthError, GmailNotConfigured, GmailScopeError

SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
# Superset of compose + readonly + trash (NOT permanent delete): drafts, send, reply tracking, test cleanup.
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
# Scopes requested by ``google-auth``. gmail.modify already covers reply-tracking reads.
SCOPES: tuple[str, ...] = (SCOPE_MODIFY, "openid", "https://www.googleapis.com/auth/userinfo.email")
REQUIRED_FOR: dict[str, tuple[str, ...]] = {
    "drafts": (SCOPE_COMPOSE,),
    "read": (SCOPE_READONLY,),  # reply tracking (read-only); gmail.modify covers it
    "trash": (SCOPE_MODIFY,),
}
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
_REAUTH = "run `uv run python -m sales0.cli google-auth` again"


@dataclass
class StoredToken:
    client_id: str
    client_secret: str
    refresh_token: str
    scopes: list[str]
    email: str | None = None
    token_uri: str = TOKEN_URI

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


def client_secret_missing_message(path: Path) -> str:
    return (
        f"Google OAuth client secret not found at {path}. In Google Cloud Console create an "
        "OAuth client ID of type 'Desktop app', download its JSON and save it to that exact path "
        "(see docs/gmail-setup.md), then run `uv run python -m sales0.cli google-auth`."
    )


def read_client_secret(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise GmailNotConfigured(client_secret_missing_message(path))
    try:
        data = json.loads(path.read_text("utf-8"))
        inner = data.get("installed") or data["web"]
        inner["client_id"], inner["client_secret"]  # noqa: B018 — presence check
    except (ValueError, KeyError, AttributeError) as exc:
        raise GmailNotConfigured(
            f"{path} is not a Google OAuth client JSON (expected an 'installed' block with "
            "client_id and client_secret). Re-download it from Credentials -> your Desktop client."
        ) from exc
    return inner


def save_token(path: Path, token: StoredToken) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token.to_json())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_token(path: Path) -> StoredToken:
    if not path.is_file():
        raise GmailNotConfigured(f"No Gmail token at {path}: {_REAUTH}.")
    try:
        return StoredToken(**json.loads(path.read_text("utf-8")))
    except (ValueError, TypeError) as exc:
        raise GmailNotConfigured(f"The Gmail token file {path} is unreadable: {_REAUTH}.") from exc


def run_consent_flow(client_secret_path: Path, token_path: Path, *, open_browser: bool = True) -> StoredToken:
    """Open the browser consent screen, store and return the token. Real Google; not unit-tested."""
    read_client_secret(client_secret_path)  # clear error before opening a browser
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(SCOPES))
    creds = flow.run_local_server(
        host="localhost",
        port=0,  # a free port
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",  # always yields a refresh token
        authorization_prompt_message="Opening your browser for Google consent. If it does not open, visit:\n{url}",
        success_message="sales0: Google sign-in complete. You can close this tab.",
    )
    if not creds.refresh_token:
        raise GmailAuthError(
            "Google returned no refresh token. Remove sales0 at https://myaccount.google.com/permissions "
            f"and {_REAUTH}."
        )
    inner = read_client_secret(client_secret_path)
    granted = sorted(creds.granted_scopes or creds.scopes or SCOPES)
    token = StoredToken(inner["client_id"], inner["client_secret"], creds.refresh_token, granted)
    with httpx.Client(timeout=20) as http:
        r = http.get(USERINFO_URI, headers={"Authorization": f"Bearer {creds.token}"})
    if r.status_code == 200:
        token.email = r.json().get("email")
    save_token(token_path, token)
    return token


@dataclass
class TokenProvider:
    """Hands out a valid access token, refreshing transparently."""

    token: StoredToken
    http: httpx.Client
    clock: Callable[[], float] = time.time
    _access: str | None = field(default=None, init=False)
    _expires_at: float = field(default=0.0, init=False)

    def require_scopes(self, feature: str) -> None:
        granted = set(self.token.scopes)
        if SCOPE_MODIFY in granted:  # gmail.modify covers compose and readonly
            granted |= {SCOPE_COMPOSE, SCOPE_READONLY}
        missing = [s for s in REQUIRED_FOR.get(feature, ()) if s not in granted]
        if missing:
            raise GmailScopeError(
                f"The stored Google token lacks the scope for {feature} ({', '.join(missing)}): {_REAUTH}."
            )

    def access_token(self, *, force: bool = False) -> str:
        if not force and self._access and self.clock() < self._expires_at - 60:
            return self._access
        try:
            r = self.http.post(
                self.token.token_uri,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.token.refresh_token,
                    "client_id": self.token.client_id,
                    "client_secret": self.token.client_secret,
                },
            )
        except httpx.HTTPError as exc:
            from sales0.integrations.gmail.errors import GmailTransientError

            raise GmailTransientError(f"Could not reach Google to refresh the token: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            try:
                err = r.json().get("error", "")
            except ValueError:
                err = ""
            if r.status_code in (400, 401) or err in ("invalid_grant", "invalid_client"):
                hint = (
                    "The Google token was revoked or has expired (Testing-mode refresh tokens last 7 days)"
                    if err == "invalid_grant"
                    else f"Google rejected the stored credentials ({err or r.status_code})"
                )
                raise GmailAuthError(f"{hint}: {_REAUTH}.")
            from sales0.integrations.gmail.errors import GmailTransientError

            raise GmailTransientError(f"Token refresh failed with HTTP {r.status_code}")
        body = r.json()
        self._access = body["access_token"]
        self._expires_at = self.clock() + float(body.get("expires_in", 3600))
        return self._access

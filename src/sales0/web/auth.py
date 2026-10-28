"""Shared-password login: signed session cookie, throttle, and the gate middleware.

``SALES0_APP_PASSWORD`` unset (or empty) = the app is open (loopback use). When set, every
path except ``OPEN_PATHS`` needs a valid ``sales0_session`` cookie. See
``spec/capabilities/shared-login.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import threading
from datetime import datetime
from urllib.parse import quote, urlsplit

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sales0 import clock
from sales0.config import Settings
from sales0.logging import get_logger
from sales0.services.team import crypto
from sales0.web.envelope import err, is_htmx, wants_json

log = get_logger("sales0.auth")

COOKIE = "sales0_session"
SESSION_PURPOSE = "session"
OPEN_PATHS = ("/login", "/logout", "/api/health", "/static/")  # plus "/__fixtures/" in fixture mode
FIXTURE_PREFIX = "/__fixtures/"
DEFAULT_NEXT = "/inbox"
THROTTLE_WINDOW_S = 60
UNAUTHORIZED_MESSAGE = "Please log in."


def now() -> datetime:
    """The clock seam for sessions and the throttle (tests patch ``sales0.web.auth.now``)."""
    return clock.utcnow()


# ---------------------------------------------------------------------- password + session
def _password(settings: Settings) -> str:
    return settings.app_password.get_secret_value() if settings.app_password is not None else ""


def password_matches(settings: Settings, candidate: str) -> bool:
    """Constant-time comparison (SHA-256 digests, so lengths never leak)."""
    expected = hashlib.sha256(_password(settings).encode("utf-8")).digest()
    given = hashlib.sha256(candidate.encode("utf-8")).digest()
    return hmac.compare_digest(expected, given) and settings.app_password_set


def password_fingerprint(settings: Settings) -> str:
    """Changing the password changes this, which invalidates every existing session."""
    mac = hmac.new(crypto.master_secret(settings), b"pw:" + _password(settings).encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:12]


def issue_session(response: Response, settings: Settings, now_: datetime | None = None) -> None:
    issued_at = int((now_ or now()).timestamp())
    value = crypto.sign(f"v1:{issued_at}:{password_fingerprint(settings)}", SESSION_PURPOSE, settings)
    response.set_cookie(
        COOKIE,
        value,
        max_age=settings.session_days * 86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


def clear_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=settings.session_cookie_secure)


def session_valid(request: Request, settings: Settings, now_: datetime | None = None) -> bool:
    value = crypto.unsign(request.cookies.get(COOKIE), SESSION_PURPOSE, settings)
    if value is None:
        return False
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "v1":
        return False
    try:
        issued_at = int(parts[1])
    except ValueError:
        return False
    age = (now_ or now()).timestamp() - issued_at
    if age < -60 or age >= settings.session_days * 86400:
        return False
    return hmac.compare_digest(parts[2], password_fingerprint(settings))


# ---------------------------------------------------------------------- redirects
def safe_next(value: str | None) -> str:
    """A same-origin path only: one leading ``/``, no scheme/host, no ``//`` or backslash tricks."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_NEXT
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return DEFAULT_NEXT
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return DEFAULT_NEXT
    if parts.path == "/login" or parts.path.startswith("/logout"):
        return DEFAULT_NEXT
    return value


# ---------------------------------------------------------------------- throttle
class LoginThrottle:
    """In-memory failed-login limiter per client IP (a restart clears it)."""

    def __init__(self, max_attempts: int, lockout_s: int, window_s: int = THROTTLE_WINDOW_S) -> None:
        self.max_attempts = max_attempts
        self.lockout_s = lockout_s
        self.window_s = window_s
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, ip: str, now_: datetime) -> int | None:
        """Seconds of lockout left, or None when the client may try."""
        ts = now_.timestamp()
        with self._lock:
            until = self._locked_until.get(ip)
            if until is None:
                return None
            if ts >= until:
                del self._locked_until[ip]
                self._failures.pop(ip, None)
                return None
            return max(1, math.ceil(until - ts))

    def fail(self, ip: str, now_: datetime) -> None:
        ts = now_.timestamp()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if ts - t < self.window_s]
            recent.append(ts)
            self._failures[ip] = recent
            if len(recent) >= self.max_attempts:
                self._locked_until[ip] = ts + self.lockout_s
            if len(self._failures) > 5000:  # bounded memory under a spray from many addresses
                for key in [k for k, v in self._failures.items() if not v or ts - v[-1] >= self.window_s]:
                    self._failures.pop(key, None)
                    self._locked_until.pop(key, None)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._locked_until.pop(ip, None)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------- middleware
def is_open_path(path: str, settings: Settings) -> bool:
    prefixes = OPEN_PATHS + ((FIXTURE_PREFIX,) if settings.linkedin_mode == "fixture" else ())
    for p in prefixes:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p:
            return True
    return False


def unauthenticated_response(request: Request) -> Response:
    if is_htmx(request):
        return Response(status_code=401, headers={"HX-Redirect": "/login", "Cache-Control": "no-store"})
    if wants_json(request):
        return err("unauthorized", UNAUTHORIZED_MESSAGE, 401)
    if request.method.upper() in {"GET", "HEAD"}:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        location = f"/login?next={quote(target, safe='')}"
    else:
        location = "/login"
    return Response(status_code=303, headers={"Location": location, "Cache-Control": "no-store"})


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        enabled = self.settings.app_password_set
        request.state.auth_enabled = enabled
        if not enabled or is_open_path(request.url.path, self.settings):
            return await call_next(request)
        if session_valid(request, self.settings):
            return await call_next(request)
        return unauthenticated_response(request)


def install(app: FastAPI, settings: Settings) -> None:
    """Add the auth middleware and the login router."""
    from sales0.web.routes import auth as auth_routes

    app.state.auth_settings = settings
    app.state.login_throttle = LoginThrottle(settings.login_max_attempts, settings.login_lockout_s)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(auth_routes.router)

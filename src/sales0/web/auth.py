"""Per-user login: signed session cookie bound to a server-side session, throttle, client IP, gate middleware.

Open mode (no member has a password and ``SALES0_APP_PASSWORD`` is unset) = no login, loopback use.
Otherwise every path except ``OPEN_PATHS`` needs a live ``sales0_session``. See
``spec/capabilities/per-user-accounts.md``.
"""

from __future__ import annotations

import ipaddress
import math
import threading
from datetime import datetime
from urllib.parse import quote, urlsplit

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sales0 import clock
from sales0.config import Settings
from sales0.db.engine import session_scope
from sales0.logging import get_logger
from sales0.services.team import accounts, crypto
from sales0.web.envelope import err, is_htmx, wants_json

log = get_logger("sales0.auth")

COOKIE = "sales0_session"
SESSION_PURPOSE = "session"
OPEN_PATHS = ("/login", "/logout", "/api/health", "/static/", "/set-password/")  # plus "/__fixtures/" in fixture mode
FIXTURE_PREFIX = "/__fixtures/"
DEFAULT_NEXT = "/inbox"
THROTTLE_WINDOW_S = 60
IP_BUCKET_FACTOR = 4  # one address may fail this many times more than one (ip, email) pair before it is locked
UNAUTHORIZED_MESSAGE = "Please log in."


def now() -> datetime:
    """The clock seam for sessions and the throttle (tests patch ``sales0.web.auth.now``)."""
    return clock.utcnow()


# ---------------------------------------------------------------------- session cookie
def set_session_cookie(response: Response, settings: Settings, sid: str, expires_at: datetime, now_: datetime | None = None) -> None:
    max_age = max(1, int((expires_at - (now_ or now())).total_seconds()))
    response.set_cookie(
        COOKIE,
        crypto.sign(sid, SESSION_PURPOSE, settings),
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


def clear_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=settings.session_cookie_secure)


def cookie_sid(request: Request, settings: Settings) -> str | None:
    """The raw session id from a validly signed cookie, else None."""
    return crypto.unsign(request.cookies.get(COOKIE), SESSION_PURPOSE, settings)


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


class LoginGuard:
    """Failed-login limiter per (client IP, email) plus a looser per-IP bucket (stops spraying many emails)."""

    def __init__(self, max_attempts: int, lockout_s: int, window_s: int = THROTTLE_WINDOW_S) -> None:
        self.pair = LoginThrottle(max_attempts, lockout_s, window_s)
        self.by_ip = LoginThrottle(max_attempts * IP_BUCKET_FACTOR, lockout_s, window_s)

    @staticmethod
    def _key(ip: str, email: str) -> str:
        return f"{ip}|{email.strip().lower()}"

    def check(self, ip: str, email: str, now_: datetime) -> int | None:
        waits = [w for w in (self.pair.check(self._key(ip, email), now_), self.by_ip.check(ip, now_)) if w is not None]
        return max(waits) if waits else None

    def fail(self, ip: str, email: str, now_: datetime) -> None:
        self.pair.fail(self._key(ip, email), now_)
        self.by_ip.fail(ip, now_)

    def reset(self, ip: str, email: str) -> None:
        self.pair.reset(self._key(ip, email))  # the per-IP bucket decays on its own


# ---------------------------------------------------------------------- client address
def _parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("trusted_proxy_ignored", value=part)
    return nets


def _in(nets: list, value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return any((ip in n) or (mapped is not None and mapped in n) for n in nets)


def client_ip(request: Request, settings: Settings | None = None) -> str:
    """The real client address.

    Default: the TCP peer (uvicorn runs with ``proxy_headers=False``; X-Forwarded-For is spoofable and is
    ignored). Only when the peer is listed in ``SALES0_TRUSTED_PROXIES`` do we read X-Forwarded-For, and then
    take the RIGHT-MOST entry that is not itself a trusted proxy - entries to its left are client-supplied and
    cannot be believed. An unparsable entry is not believed either (the peer is returned)."""
    peer = request.client.host if request.client else "unknown"
    settings = settings or getattr(request.app.state, "auth_settings", None)
    nets = _parse_networks(settings.trusted_proxies) if settings is not None else []
    if not nets or not _in(nets, peer):
        return peer
    entries = [e.strip() for h in request.headers.getlist("x-forwarded-for") for e in h.split(",") if e.strip()]
    for entry in reversed(entries):
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            return peer
        if not _in(nets, entry):
            return entry
    return entries[0] if entries and _is_ip(entries[0]) else peer


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


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
    """Sets ``request.state.auth_enabled`` / ``login_member_id`` / ``login_session_id`` and gates closed paths."""

    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = self.settings
        request.state.login_member_id = None
        request.state.login_session_id = None
        path = request.url.path
        if path.startswith("/static/") or path == "/api/health":
            request.state.auth_enabled = True
            return await call_next(request)
        rotated: tuple[str, datetime] | None = None
        enabled = settings.app_password_set
        try:
            with session_scope() as db:
                enabled = accounts.auth_enabled(db, settings)
                if enabled:
                    sid = cookie_sid(request, settings)
                    info = accounts.resolve_session(db, sid, settings, ip=client_ip(request, settings), now=now()) if sid else None
                    if info is not None:
                        request.state.login_member_id = info.member.id
                        request.state.login_session_id = info.session_id
                        if info.new_sid:
                            rotated = (info.new_sid, info.expires_at)
        except SQLAlchemyError:
            # No readable accounts table (an unmigrated DB: startup refuses that, tests may build a bare app).
            # Fall back to the bootstrap variable alone; with it set nobody can be logged in, so the gate holds.
            log.warning("auth_state_unavailable", exc_info=True)
        request.state.auth_enabled = enabled
        if not enabled:
            return await call_next(request)
        if request.state.login_member_id is None and not is_open_path(path, settings):
            response = unauthenticated_response(request)
            if request.cookies.get(COOKIE):
                clear_session(response, settings)  # a dead cookie: stop sending it
            return response
        response = await call_next(request)
        if rotated is not None and not any(c.startswith(f"{COOKIE}=") for c in response.headers.getlist("set-cookie")):
            set_session_cookie(response, settings, rotated[0], rotated[1])
        return response


def install(app: FastAPI, settings: Settings) -> None:
    """Add the auth middleware and the login router."""
    from sales0.web.routes import auth as auth_routes

    app.state.auth_settings = settings
    app.state.login_throttle = LoginGuard(settings.login_max_attempts, settings.login_lockout_s)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(auth_routes.router)

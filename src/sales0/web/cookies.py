"""One place that decides the ``Secure`` flag of every cookie sales0 sets.

``SALES0_COOKIE_SECURE``: ``true`` / ``false`` force it; ``auto`` (default) sets it when the request itself arrived
over https, or ``X-Forwarded-Proto: https`` came from a peer listed in ``SALES0_TRUSTED_PROXIES``, or
``SALES0_PUBLIC_BASE_URL`` is https.

The decision is bound once per request by the outermost middleware (``bind_request``) so helpers called from
anywhere in the request (``set_acting_cookie``, ``set_flash``, the session cookie) need no extra argument.
"""

from __future__ import annotations

from contextvars import ContextVar

from starlette.requests import Request

from sales0.config import Settings, get_settings

_bound: ContextVar[bool | None] = ContextVar("sales0_cookie_secure", default=None)


def _settings_for(request: Request | None) -> Settings:
    if request is not None:
        found = getattr(request.app.state, "auth_settings", None) if "app" in request.scope else None
        if found is not None:
            return found
    return get_settings()


def cookie_secure(request: Request | None = None, settings: Settings | None = None) -> bool:
    """Whether cookies set for ``request`` carry ``Secure`` (falls back to the request bound to this context)."""
    settings = settings or _settings_for(request)
    if settings.cookie_secure != "auto":
        return settings.cookie_secure == "true"
    if request is None:
        bound = _bound.get()
        return settings.session_cookie_secure if bound is None else bound
    if (settings.public_base_url or "").lower().startswith("https://"):
        return True
    if request.scope.get("scheme") in {"https", "wss"}:
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        from sales0.web.auth import _in, _parse_networks

        peer = request.client.host if request.client else ""
        nets = _parse_networks(settings.trusted_proxies)
        if nets and peer and _in(nets, peer):
            # the proxy appends its own view last; the right-most value is the one it vouches for
            return forwarded.split(",")[-1].strip().lower() == "https"
    return False


def bind_request(request: Request) -> None:
    _bound.set(cookie_secure(request))


def set_cookie(response, key: str, value: str, request: Request | None = None, settings: Settings | None = None, **kwargs) -> None:  # noqa: ANN001, ANN003
    kwargs.setdefault("httponly", True)
    kwargs.setdefault("samesite", "lax")
    kwargs.setdefault("path", "/")
    response.set_cookie(key, value, secure=cookie_secure(request, settings), **kwargs)


def delete_cookie(response, key: str, request: Request | None = None, settings: Settings | None = None, **kwargs) -> None:  # noqa: ANN001, ANN003
    kwargs.setdefault("path", "/")
    response.delete_cookie(key, secure=cookie_secure(request, settings), **kwargs)

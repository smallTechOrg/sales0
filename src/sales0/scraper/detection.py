"""Logged-in + account-safety detection (architecture.md → "Logged-in detection").

``classify_page`` is THE classifier for live pages and tests. It is pure and decides from
the primary signals: the ``li_at`` session cookie, the final URL path and the main-document
HTTP status. DOM markers count only as additional LOGGED-OUT signals (login form, authwall,
captcha); logged-in markers are soft hints that are reported and never part of a verdict.

``read_signals`` / ``session_cookie_present`` read the signals off a live Playwright page
in ONE ``page.evaluate(SIGNALS_JS, SIGNAL_ARGS)`` plus one ``context.cookies(base)``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sales0.scraper import selectors as S
from sales0.scraper.parsing import CARD_ARGS, CARDS_PRELUDE_JS
from sales0.scraper.urls import linkedin_path, same_site

__all__ = [
    "CONTENT_READY_JS", "SIGNALS_JS", "SIGNAL_ARGS", "PageKind", "PageSignals", "PageVerdict", "classify_page",
    "classify_url", "is_rate_limit_response", "linkedin_path", "read_signals", "same_site", "session_cookie_present",
    "session_cookie_valid", "signals_from",
]

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Response

PageKind = Literal["ok", "logout", "checkpoint", "captcha", "rate_limited", "http_error"]
UrlKind = Literal["logout", "checkpoint", "captcha"]

RATE_LIMIT_STATUSES: frozenset[int] = frozenset({429, 999})
LOGIN_FIRST_SEGMENTS: frozenset[str] = frozenset({"login", "login-submit", "uas", "authwall", "signup", "signin", "reg"})
LOGIN_PATH_PREFIXES: tuple[str, ...] = ("/checkpoint/lg/", "/checkpoint/rm/sign-in-another-account", "/m/login")
CHECKPOINT_FIRST_SEGMENT = "checkpoint"


@dataclass(frozen=True)
class PageSignals:
    url: str  # final URL
    status: int | None  # main-document status (None = unknown)
    has_session_cookie: bool  # non-expired li_at applicable to base
    has_captcha: bool
    has_login_form: bool
    has_authwall: bool
    soft_hints: tuple[str, ...] = ()  # NEVER affects the verdict


@dataclass(frozen=True)
class PageVerdict:
    kind: PageKind
    reason: str  # e.g. "path:/authwall", "status:429", "no_cookie", "login_form", "ok"


# --------------------------------------------------------------------------- URL rules
# ``linkedin_path`` / ``same_site`` live in ``scraper.urls`` (pure, shared) and are re-exported here.
def classify_url(url: str, base: str | None = None) -> UrlKind | None:
    """Path-only rules (the navigation latch, the Connect poll, ``classify_page``).

    The query is never inspected, so a search keyword like "login" never trips it. With
    ``base``, a URL that is not ``same_site`` with it is None. First match wins: captcha,
    logout (login / challenge-to-login paths), checkpoint.
    """
    if base is not None and not same_site(url, base):
        return None
    path = linkedin_path(url)
    if path is None:
        return None
    lowered = path.lower()
    segments = [part for part in lowered.split("/") if part]
    if any("captcha" in seg for seg in segments):
        return "captcha"
    if segments and segments[0] in LOGIN_FIRST_SEGMENTS:
        return "logout"
    if any(lowered.startswith(prefix) for prefix in LOGIN_PATH_PREFIXES):
        return "logout"
    if segments and segments[0] == CHECKPOINT_FIRST_SEGMENT:
        return "checkpoint"
    return None


def classify_page(signals: PageSignals) -> PageVerdict:
    """The ONE classifier (first match wins; see the rules table in architecture.md)."""
    status = signals.status
    if status in RATE_LIMIT_STATUSES:
        return PageVerdict("rate_limited", f"status:{status}")
    url_kind = classify_url(signals.url)
    path = linkedin_path(signals.url) or signals.url
    if signals.has_captcha:
        return PageVerdict("captcha", "captcha_marker")
    if url_kind == "captcha":
        return PageVerdict("captcha", f"path:{path}")
    if url_kind == "checkpoint":
        return PageVerdict("checkpoint", f"path:{path}")
    if url_kind == "logout":
        return PageVerdict("logout", f"path:{path}")
    if not signals.has_session_cookie:
        return PageVerdict("logout", "no_cookie")
    if signals.has_login_form:
        return PageVerdict("logout", "login_form")
    if signals.has_authwall:
        return PageVerdict("logout", "authwall")
    if status is not None and status >= 400:
        return PageVerdict("http_error", f"status:{status}")
    return PageVerdict("ok", "ok")


# --------------------------------------------------------------------------- site rules
def is_rate_limit_response(url: str, status: int, base: str) -> bool:
    """A response (document, XHR or fetch) that means LinkedIn is throttling this session."""
    return status in RATE_LIMIT_STATUSES and same_site(url, base)


# --------------------------------------------------------------------------- cookie
def session_cookie_valid(cookies: Iterable[Mapping[str, Any]], now: float | None = None) -> bool:
    """Pure: a non-empty, non-expired ``li_at`` among ``cookies`` (Playwright cookie dicts).

    Non-expired = ``expires == -1`` (a session cookie) or ``expires > now``.
    """
    current = time.time() if now is None else now
    for cookie in cookies:
        if cookie.get("name") != S.SESSION_COOKIE or not cookie.get("value"):
            continue
        expires = cookie.get("expires", -1)
        if expires is None or expires == -1 or float(expires) > current:
            return True
    return False


def session_cookie_present(context: BrowserContext, base: str, now: float | None = None) -> bool:
    """A non-expired ``li_at`` applicable to ``base`` in the context (no navigation)."""
    return session_cookie_valid(context.cookies(base), now)


# --------------------------------------------------------------------------- live page
SIGNAL_ARGS: dict[str, Any] = {
    **CARD_ARGS,
    "login_form": list(S.LOGIN_FORM_MARKERS),
    "authwall": list(S.AUTHWALL_MARKERS),
    "captcha": list(S.CAPTCHA_MARKERS),
    "soft_hints": dict(S.SOFT_LOGGED_IN_HINTS),
    "sdui_screen": S.SDUI_SCREEN,  # Phase 1.2: informational only
}

# () => {has_captcha, has_login_form, has_authwall, no_results, cards_found, strategy, sdui_screen, soft_hints}
SIGNALS_JS = (
    "(sel) => {"
    + CARDS_PRELUDE_JS
    + r"""
return {
  has_captcha: __any(sel.captcha),
  has_login_form: __any(sel.login_form),
  has_authwall: __any(sel.authwall),
  no_results: __noResults(sel),
  cards_found: __cardsFound(sel),
  strategy: __cards(sel).strategy,
  sdui_screen: !!document.querySelector(sel.sdui_screen),
  soft_hints: Object.entries(sel.soft_hints).filter(([, css]) => __any([css])).map(([name]) => name),
};
}"""
)

# Content wait (searcher): cards, the no-results marker, or a logged-out/captcha marker appeared.
CONTENT_READY_JS = (
    "(sel) => {"
    + CARDS_PRELUDE_JS
    + "return __cardsFound(sel) > 0 || __noResults(sel) || __any(sel.captcha) || __any(sel.login_form)"
    + " || __any(sel.authwall); }"
)


def signals_from(url: str, status: int | None, has_cookie: bool, dom: Mapping[str, Any]) -> tuple[PageSignals, dict]:
    """Pure: build ``(PageSignals, extras)`` from one SIGNALS_JS result."""
    signals = PageSignals(
        url=url,
        status=status,
        has_session_cookie=has_cookie,
        has_captcha=bool(dom.get("has_captcha")),
        has_login_form=bool(dom.get("has_login_form")),
        has_authwall=bool(dom.get("has_authwall")),
        soft_hints=tuple(str(h) for h in (dom.get("soft_hints") or ())),
    )
    extras = {"no_results": bool(dom.get("no_results")), "cards_found": int(dom.get("cards_found") or 0)}
    # Phase 1.2 (informational, logged only; never part of a verdict).
    if "strategy" in dom:
        extras["strategy"] = str(dom.get("strategy") or "none")
    if "sdui_screen" in dom:
        extras["sdui_screen"] = bool(dom.get("sdui_screen"))
    return signals, extras


def read_signals(page: Page, response: Response | None, context: BrowserContext, base: str) -> tuple[PageSignals, dict]:
    """Cookie + ONE SIGNALS_JS evaluation → ``(signals, extras)``. Raises PlaywrightError mid-navigation."""
    dom = page.evaluate(SIGNALS_JS, SIGNAL_ARGS)
    status = response.status if response is not None else None
    return signals_from(page.url, status, session_cookie_present(context, base), dom or {})

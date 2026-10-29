"""The ONE canonicaliser for LinkedIn profile URLs / slugs.

Rules (audit #5 + #13): percent-decode each path segment, Unicode NFC, ``casefold``, host must be
``linkedin.com`` or ``*.linkedin.com`` (the app's own ``/__fixtures/linkedin`` routes only when
``allow_fixture``), a scheme-less ``linkedin.com/in/x`` is accepted, short links (``lnkd.in``) are NOT
resolved (``None``). Kinds: ``in`` (person), ``pub`` (legacy person), ``company``, ``school``.

Pure and dependency-free (it is imported by the scraper, the CRM and the sheet sync).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

KINDS = ("in", "company", "school", "pub")
FIXTURE_PREFIX = "/__fixtures/linkedin"
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _host_ok(host: str) -> bool:
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _segment(raw: str) -> str:
    return unicodedata.normalize("NFC", unquote(raw)).strip().casefold()


def canonical_linkedin(
    url: str | None, *, allow_fixture: bool = False, allow_relative: bool = False
) -> tuple[str, str] | None:
    """``(kind, slug)`` of a LinkedIn profile link, or ``None``.

    ``allow_fixture`` additionally accepts the app's fixture LinkedIn (any host, path starting with
    ``/__fixtures/linkedin``); ``allow_relative`` accepts a site-relative path (``/in/x/``) as found in
    scraped card hrefs.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    text = text.split()[0]
    if text.startswith("//"):
        text = "https:" + text
    relative = text.startswith("/")
    if relative:
        if not allow_relative:
            return None
        text = "https://www.linkedin.com" + text
    elif not _SCHEME.match(text):
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    path = parts.path or "/"
    if path == FIXTURE_PREFIX or path.startswith(FIXTURE_PREFIX + "/"):
        if not allow_fixture and not relative:
            return None
        path = path[len(FIXTURE_PREFIX) :] or "/"
    elif not _host_ok(host):
        return None
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return None
    kind = segments[0].lower()
    if kind not in KINDS:
        return None
    slug = _segment(segments[1])
    if not slug:
        return None
    return kind, slug


def linkedin_slug(url: str | None, **kw: bool) -> str | None:
    """Slug only (``…/in/Pat-123/?x`` -> ``pat-123``); the kind is dropped (see :func:`canonical_linkedin`)."""
    found = canonical_linkedin(url, **kw)
    return found[1] if found else None


def profile_slug(url: str | None, **kw: bool) -> str | None:
    """``in/<slug>`` or ``company/<slug>`` for the profile kinds the app tracks; ``None`` otherwise."""
    found = canonical_linkedin(url, **kw)
    if found is None or found[0] not in ("in", "company"):
        return None
    return f"{found[0]}/{found[1]}"


def key_of(url: str | None) -> str | None:
    """The sheet-key body: bare slug for a person (``in``, the historical ``li:<slug>`` form), else ``<kind>/<slug>``."""
    found = canonical_linkedin(url)
    if found is None:
        return None
    kind, slug = found
    return slug if kind == "in" else f"{kind}/{slug}"


__all__ = ["KINDS", "canonical_linkedin", "key_of", "linkedin_slug", "profile_slug"]

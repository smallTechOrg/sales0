"""Pure key/normalisation helpers of the CRM.

``migrations/versions/p6_crm_5b2e7d14.py`` carries FROZEN copies of ``name_key``, ``phone_digits``,
``first_email`` and ``search_text``; ``tests/unit/crm/test_keys.py`` asserts they stay equal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from sales0.linkedin_url import canonical_linkedin, profile_slug

_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"[^\s,;<>()\[\]\"']+@[^\s,;<>()\[\]\"']+\.[A-Za-z]{2,}")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def collapse(text: str | None) -> str:
    return _WS.sub(" ", (text or "").strip())


def name_key(text: str | None) -> str:
    """``lower(trim(...))`` with inner whitespace collapsed (companies, tags, competitors, groups)."""
    return collapse(text).lower()


def phone_digits(phone: str | None) -> str:
    """The last 10 digits of a phone number (the matching key of ``contact_phones.digits``)."""
    return re.sub(r"\D", "", phone or "")[-10:]


def first_email(text: str | None) -> str | None:
    """The first plausible e-mail address in ``text``, lower-cased; None if there is none."""
    m = _EMAIL.search(text or "")
    return m.group(0).strip(".").lower() if m else None


def all_emails(text: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for m in _EMAIL.finditer(text or ""):
        seen.setdefault(m.group(0).strip(".").lower(), None)
    return list(seen)


def display_phone(phone: str | None) -> str | None:
    """``+<digits>`` form when the number is plausible, else the trimmed text, else None."""
    text = collapse(phone)
    if not text:
        return None
    return normalise_phone(text) or (text if len(phone_digits(text)) >= 6 else None)


def search_text(full_name: str | None, emails: Sequence[str], company: str | None, extra: Sequence[str] = ()) -> str:
    parts = [full_name or "", *emails, company or "", *extra]
    return " ".join(p for p in parts if p).strip().lower()[:700]


def linkedin_slug_of(url_or_slug: str | None) -> str | None:
    """``https://www.linkedin.com/in/x/`` / ``linkedin.com/in/x`` / ``in/x`` -> ``in/x`` (None if not a profile).

    Delegates to the one canonicaliser, :mod:`sales0.linkedin_url` (percent-decoding, NFC, casefold, host check)."""
    text = (url_or_slug or "").strip()
    if not text:
        return None
    if text.lower().startswith(("in/", "company/")):
        text = "linkedin.com/" + text
    return profile_slug(text, allow_fixture=True)


def is_linkedin_url(url: str) -> bool:
    """linkedin.com (any subdomain) or the app's own fixture LinkedIn."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com") or parts.path.startswith("/__fixtures/linkedin")


def name_tokens(name: str | None) -> list[str]:
    return _TOKEN.findall((name or "").casefold())


def names_match_loosely(a: str | None, b: str | None) -> bool:
    """Same person by name: equal tokens, one a subset of the other, or same first and last token."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    sa, sb = set(ta), set(tb)
    if sa == sb or sa <= sb or sb <= sa:
        return True
    return ta[0] == tb[0] and ta[-1] == tb[-1]


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- copies of sheetsync.values helpers (importing that package here would create an import cycle:
# --- sheetsync -> sql_repository -> crm). Kept behaviourally identical; see tests/unit/crm/test_keys.py.
_URL_LIKE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|trk|trackingid|originalsubdomain)", re.IGNORECASE)


def normalise_phone(candidate: str) -> str | None:
    plus = candidate.strip().startswith("+")
    digits = re.sub(r"\D", "", candidate)
    if plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        return f"+91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return digits
    return None


def looks_like_url(text: str | None) -> bool:
    return bool(text and _URL_LIKE.match(text.strip()))


def normalize_url(text: str | None) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    t = t.split()[0]
    if not looks_like_url(t):
        return None
    if t.lower().startswith("www."):
        t = "https://" + t
    try:
        parts = urlsplit(t)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    path = parts.path.rstrip("/")
    query = "&".join(sorted(p for p in parts.query.split("&") if p and not _TRACKING.match(p)))
    out = f"{host}{path}"
    return f"{out}?{query}" if query else out


def website_key(text: str | None) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    t = t.split()[0]
    if "." not in t or "@" in t:
        return None
    if not looks_like_url(t):
        t = "https://" + t
    return normalize_url(t)


def website_host(text: str | None) -> str | None:
    key = website_key(text)
    return key.split("/", 1)[0].split("?", 1)[0] if key else None


# --- company matching keys (q12). FROZEN copies live in migrations/versions/q12_traits_9d2c4a71.py (backfill);
# --- tests/unit/crm/test_company_keys.py asserts they stay equal.
#: Hosts that never identify one company (shared platforms, mail providers, link shorteners).
GENERIC_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be",
    "google.com", "sites.google.com", "docs.google.com", "forms.gle", "gmail.com", "yahoo.com", "outlook.com",
    "hotmail.com", "wa.me", "whatsapp.com", "chat.whatsapp.com", "bit.ly", "lnkd.in", "t.me", "medium.com",
    "github.com", "wordpress.com", "blogspot.com", "wixsite.com", "linktr.ee",
})


def domain_key(website: str | None) -> str | None:
    """The bare host of a company website (``https://www.Acme.com/about`` -> ``acme.com``); None for a
    generic platform host (a LinkedIn / Facebook page, a gmail address...) that cannot identify one company."""
    host = website_host(website)
    if not host or host in GENERIC_HOSTS or "." not in host:
        return None
    return host[:200]


def company_linkedin_key(url: str | None) -> str | None:
    """``company/<slug>`` for a LinkedIn company page (``linkedin.com/company/acme-ltd/about``); else None."""
    found = canonical_linkedin(url)
    if found is None or found[0] != "company":
        return None
    return f"company/{found[1]}"[:200]

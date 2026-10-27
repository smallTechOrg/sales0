"""The ONE post parser, shared by live and fixture mode.

``EXTRACT_JS`` pulls raw fields out of every result card using the selectors in
``selectors.py``; ``post_from_raw`` (pure) validates and normalises one card into a
``ScrapedPost``. A card that cannot be parsed raises ``ParseError`` and is counted.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from sales0.scraper import selectors
from sales0.scraper.relative_time import normalize_relative, parse_relative
from sales0.scraper.types import ScrapedPost
from sales0.scraper.urls import permalink

URN_RE = re.compile(r"^urn:li:(?:activity|ugcPost|share):\d+$")
_TOGGLE_RE = re.compile(r"^(…|\.\.\.)?\s*see (more|less)$", re.IGNORECASE)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


class ParseError(ValueError):
    """One result card could not be turned into a post."""


# Runs in the page: (cards, sel) => list of raw dicts. innerText keeps visible line breaks.
# A card nested inside another matched card (e.g. the reshared original inside a repost)
# belongs to its outer card and is skipped, so one visible post is never parsed twice.
EXTRACT_JS = """
(cards, sel) => cards.filter((card) => !(card.parentElement && card.parentElement.closest(sel.result_card))).map((card) => {
  const pick = (css) => card.querySelector(css);
  const txt = (css) => { const el = pick(css); return el ? (el.innerText || el.textContent || '') : null; };
  const link = pick(sel.author_profile_link);
  return {
    urn: card.getAttribute(sel.urn_attribute),
    author_name: txt(sel.author_name),
    author_profile_url: link ? link.getAttribute('href') : null,
    author_headline: txt(sel.author_headline),
    posted_relative: txt(sel.relative_time),
    text: txt(sel.post_text),
  };
})
"""

EXTRACT_ARGS: dict[str, str] = {
    "result_card": selectors.RESULT_CARD,
    "urn_attribute": selectors.URN_ATTRIBUTE,
    "author_name": selectors.AUTHOR_NAME,
    "author_profile_link": selectors.AUTHOR_PROFILE_LINK,
    "author_headline": selectors.AUTHOR_HEADLINE,
    "relative_time": selectors.RELATIVE_TIME,
    "post_text": selectors.POST_TEXT,
}


def _clean_line_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def clean_text(value: str) -> str:
    """Normalise whitespace while keeping line structure; drop see-more/less toggles."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    lines = [line.rstrip() for line in value.split("\n")]
    while lines and (not lines[-1].strip() or _TOGGLE_RE.match(lines[-1].strip())):
        lines.pop()
    text = "\n".join(lines).strip()
    return _MANY_BLANK_LINES.sub("\n\n", text)


def _profile_url(href: Any, base: str) -> str | None:
    if not isinstance(href, str) or not href.strip():
        return None
    parts = urlsplit(urljoin(base + "/", href.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def post_from_raw(raw: dict[str, Any], base: str, today: date) -> ScrapedPost:
    urn = raw.get("urn")
    if not isinstance(urn, str) or not URN_RE.match(urn.strip()):
        raise ParseError(f"card has no post URN: {urn!r}")
    urn = urn.strip()
    text_raw = raw.get("text")
    text = clean_text(text_raw) if isinstance(text_raw, str) else ""
    if not text:
        raise ParseError(f"card {urn} has no post text")
    relative = normalize_relative(_clean_line_value(raw.get("posted_relative")))
    return ScrapedPost(
        urn=urn,
        url=permalink(base, urn),
        author_name=_clean_line_value(raw.get("author_name")),
        author_profile_url=_profile_url(raw.get("author_profile_url"), base),
        author_headline=_clean_line_value(raw.get("author_headline")),
        posted_relative=relative,
        posted_date=parse_relative(relative, today),
        text=text,
    )

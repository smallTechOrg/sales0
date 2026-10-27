"""The ONE post parser, shared by live and fixture mode (architecture.md → Post parsing).

``EXTRACT_JS`` discovers the cards itself (URN attributes, then post links) and pulls every
field through ordered fallbacks from ``selectors.py``; class-based selectors survive only as
the LAST fallback (``LEGACY_*``). ``post_from_raw`` (pure) validates and normalises one card
into a ``ScrapedPost``; ``parse_cards`` (pure) does a whole page: dedupe, cap, parse errors
and ``field_misses``. A card that cannot be parsed raises ``ParseError`` and is counted.

The same card discovery (``CARDS_PRELUDE_JS``) backs ``detection.SIGNALS_JS`` (``cards_found``)
and the searcher's wait/scroll counters, so "cards found" means one thing everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from sales0.scraper import selectors as S
from sales0.scraper.relative_time import normalize_relative, parse_relative
from sales0.scraper.types import OPTIONAL_POST_FIELDS, ScrapedPost
from sales0.scraper.urls import permalink

URN_RE = re.compile(r"^urn:li:(?:activity|ugcPost|share):\d+$")
FEED_UPDATE_HREF = re.compile(S.FEED_UPDATE_HREF_RE)
POSTS_SLUG_HREF = re.compile(S.POSTS_SLUG_HREF_RE)
_TOGGLE_RE = re.compile(r"^(…|\.\.\.)?\s*((see|show)\s+)?(more|less)$", re.IGNORECASE)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


class ParseError(ValueError):
    """One result card could not be turned into a post."""


# --------------------------------------------------------------------------- JS
# Shared in-page helpers. ``sel`` is CARD_ARGS (or a superset of it).
CARDS_PRELUDE_JS = r"""
const __scope = () => document.querySelector('main') || document;
const __cards = (sel) => {
  const scope = __scope();
  if (!scope || !scope.querySelectorAll) return [];
  const found = [];
  for (const el of scope.querySelectorAll(sel.card_attr)) {
    if (!(el.parentElement && el.parentElement.closest(sel.card_attr))) found.push(el);
  }
  for (const a of scope.querySelectorAll(sel.post_links)) {
    if (found.some((c) => c.contains(a))) continue;
    const c = a.closest(sel.containers);
    // No container, already a card, or it wraps a card found by attribute (the link is that card's).
    if (!c || found.includes(c) || found.some((f) => c.contains(f))) continue;
    found.push(c);
  }
  const cards = found.filter((c) => !found.some((o) => o !== c && o.contains(c)));
  cards.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  return cards;
};
const __noResults = (sel) => {
  const scope = __scope();
  if (!scope || !scope.querySelector) return false;
  if (scope.querySelector(sel.legacy_no_results)) return true;
  for (const el of scope.querySelectorAll(sel.no_results_tags)) {
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (t.startsWith(sel.no_results_prefix) || t === sel.no_results_exact) return true;
  }
  return false;
};
const __any = (list) => list.some((s) => { try { return !!document.querySelector(s); } catch (e) { return false; } });
"""

CARD_ARGS: dict[str, Any] = {
    "card_attr": S.join(S.CARD_ATTR_SELECTORS),
    "post_links": S.join(S.POST_LINK_SELECTORS),
    "containers": S.CARD_CONTAINERS,
    "legacy_no_results": S.LEGACY_NO_RESULTS,
    "no_results_tags": S.NO_RESULTS_TEXT_TAGS,
    "no_results_prefix": S.NO_RESULTS_PREFIX,
    "no_results_exact": S.NO_RESULTS_EXACT,
}

# Number of cards currently discovered (wait-for-more-cards polling).
COUNT_CARDS_JS = "(sel) => {" + CARDS_PRELUDE_JS + "return __cards(sel).length; }"

# Per-card field extraction. Returns one raw dict per discovered card (DOM order).
EXTRACT_JS = (
    "(sel) => {"
    + CARDS_PRELUDE_JS
    + r"""
const clean = (s) => (typeof s === 'string' ? s.replace(/\s+/g, ' ').trim() : '');
const lines = (el) => (el ? (el.innerText || el.textContent || '') : '').split('\n').map(clean).filter(Boolean);
const own = (el) => clean(Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' '));
const prefixes = sel.urn_prefixes;
const validUrn = (v) => (typeof v === 'string' && prefixes.some((p) => v.trim().startsWith(p)) ? v.trim() : null);
const relShort = new RegExp(sel.rel_short, 'i');
const relLong = new RegExp(sel.rel_long, 'i');
const isRel = (t) => {
  if (!t) return false;
  const head = t.replace(/^\s*edited\s*[•·]\s*/i, '').split('•')[0].trim();
  return /^(just )?now$/i.test(head) || relShort.test(head) || relLong.test(head);
};
const skipRe = new RegExp(sel.headline_skip_re, 'i');
const feedRe = new RegExp(sel.feed_re);
const postsRe = new RegExp(sel.posts_re);
const decode = (h) => { try { return decodeURIComponent(h); } catch (e) { return h; } };

return __cards(sel).map((card) => {
  const nested = Array.from(card.querySelectorAll(sel.card_attr));
  const outside = (el) => !nested.some((n) => n.contains(el));
  const all = (css) => Array.from(card.querySelectorAll(css)).filter(outside);
  const first = (css) => all(css)[0] || null;
  const via = {};

  // URN: own attribute, first descendant's attribute, /feed/update/ href, /posts/ slug href.
  let urn = null;
  for (const attr of sel.urn_attributes) { urn = urn || validUrn(card.getAttribute(attr)); }
  if (urn) via.urn = 'attr';
  if (!urn) {
    const d = card.querySelector(sel.card_attr);
    if (d) { for (const attr of sel.urn_attributes) { urn = urn || validUrn(d.getAttribute(attr)); } }
    if (urn) via.urn = 'descendant_attr';
  }
  let postHref = null;
  const hrefs = all('a[href]').map((a) => decode(a.getAttribute('href') || ''));
  for (const h of hrefs) { const m = h.match(feedRe); if (m) { postHref = postHref || h; if (!urn) { urn = m[1]; via.urn = 'feed_update_href'; } break; } }
  if (!urn) {
    for (const h of hrefs) { const m = h.match(postsRe); if (m) { postHref = postHref || h; urn = 'urn:li:' + m[1] + ':' + m[2]; via.urn = 'posts_href'; break; } }
  }

  // Author anchor: first profile/company link in DOM order, outside nested reshares.
  const anchor = first(sel.author_anchor);
  let name = null;
  if (anchor) {
    for (const css of sel.name_in_anchor) {
      const el = anchor.querySelector(css);
      const t = el ? clean(el.innerText || el.textContent) : '';
      if (t) { name = t; via.author_name = css; break; }
    }
    if (!name) {
      let label = anchor.getAttribute('aria-label') || '';
      for (const s of sel.aria_strip) label = label.split(s).join(' ');
      label = clean(label);
      if (label) { name = label; via.author_name = 'aria-label'; }
    }
    if (!name) { const l = lines(anchor)[0]; if (l) { name = l; via.author_name = 'first_line'; } }
  }
  if (!name) { const el = first(sel.legacy_name); const t = el ? clean(el.innerText || el.textContent) : ''; if (t) { name = t; via.author_name = 'legacy'; } }

  let profile = null;
  if (anchor && anchor.getAttribute('href')) { profile = anchor.getAttribute('href'); via.author_profile_url = 'author_anchor'; }
  if (!profile) { const el = first(sel.legacy_link); if (el && el.getAttribute('href')) { profile = el.getAttribute('href'); via.author_profile_url = 'legacy'; } }

  // Text: the LONGEST candidate (tie → earlier selector), never inside/around the author anchor or a reshare.
  let textEl = null, textLen = -1;
  for (const css of sel.text_selectors) {
    for (const el of all(css)) {
      if (anchor && (anchor.contains(el) || el.contains(anchor))) continue;
      if (nested.some((n) => el.contains(n))) continue;
      const len = clean(el.innerText || el.textContent).length;
      if (len > textLen) { textEl = el; textLen = len; via.text = css; }
    }
  }
  const text = textEl && textLen > 0 ? (textEl.innerText || textEl.textContent || '') : null;
  if (!text) delete via.text;

  // Headline: first line of the author anchor (or its parent) that is not name/degree/Follow/time.
  const skip = (l) => (name && l.includes(name)) || skipRe.test(l) || isRel(l) || /^[•·|\-\s]+$/.test(l)
    || (/^view /i.test(l) && /profile$/i.test(l));
  let headline = null;
  if (anchor) {
    headline = lines(anchor).find((l) => !skip(l)) || null;
    if (headline) via.author_headline = 'author_anchor';
    const parent = anchor.parentElement;
    if (!headline && parent && parent !== card && !(textEl && parent.contains(textEl))) {
      headline = lines(parent).find((l) => !skip(l)) || null;
      if (headline) via.author_headline = 'author_parent';
    }
  }
  if (!headline) { const el = first(sel.legacy_headline); const t = el ? clean(el.innerText || el.textContent) : ''; if (t) { headline = t; via.author_headline = 'legacy'; } }

  // Relative time: <time>; first element before the text block with a time-like own text/aria-label; legacy.
  let posted = null;
  const timeEl = first('time');
  if (timeEl) { posted = clean(timeEl.innerText || timeEl.textContent) || clean(timeEl.getAttribute('datetime')) || null; if (posted) via.posted_relative = 'time'; }
  if (!posted) {
    for (const el of all('*')) {
      if (textEl && (el === textEl || textEl.contains(el) || (textEl.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))) break;
      const t = own(el);
      if (t && t !== name && isRel(t)) { posted = t; via.posted_relative = 'own_text'; break; }
      const label = clean(el.getAttribute('aria-label'));
      if (label && label !== name && isRel(label)) { posted = label; via.posted_relative = 'aria-label'; break; }
    }
  }
  if (!posted) { const el = first(sel.legacy_time); const t = el ? clean(el.innerText || el.textContent) : ''; if (t) { posted = t; via.posted_relative = 'legacy'; } }

  return { urn, post_href: postHref, author_name: name, author_profile_url: profile, author_headline: headline,
           posted_relative: posted, text, via };
});
}"""
)

EXTRACT_ARGS: dict[str, Any] = {
    **CARD_ARGS,
    "urn_attributes": list(S.URN_ATTRIBUTES),
    "urn_prefixes": list(S.URN_PREFIXES),
    "feed_re": S.FEED_UPDATE_HREF_RE,
    "posts_re": S.POSTS_SLUG_HREF_RE,
    "author_anchor": S.AUTHOR_ANCHOR,
    "name_in_anchor": list(S.AUTHOR_NAME_IN_ANCHOR),
    "aria_strip": list(S.AUTHOR_ARIA_LABEL_STRIP),
    "legacy_name": S.LEGACY_AUTHOR_NAME,
    "legacy_link": S.LEGACY_AUTHOR_PROFILE_LINK,
    "legacy_headline": S.LEGACY_AUTHOR_HEADLINE,
    "legacy_time": S.LEGACY_RELATIVE_TIME,
    "headline_skip_re": S.HEADLINE_SKIP_RE,
    "text_selectors": list(S.TEXT_SELECTORS),
    "rel_short": S.RELATIVE_TIME_SHORT_RE,
    "rel_long": S.RELATIVE_TIME_LONG_RE,
}

# The "see more" toggle of each of the first ``n`` cards (an array of elements; cards without one are skipped).
SEE_MORE_JS = (
    "(sel) => {"
    + CARDS_PRELUDE_JS
    + r"""
const textRe = new RegExp(sel.see_more_text_re, 'i');
const excludeRe = new RegExp(sel.see_more_exclude_re, 'i');
const ok = (el) => !(el.tagName === 'A' && el.hasAttribute('href')) && !excludeRe.test(el.getAttribute('aria-label') || '');
const out = [];
for (const card of __cards(sel).slice(0, sel.n)) {
  const nested = Array.from(card.querySelectorAll(sel.card_attr));
  const outside = (el) => !nested.some((n) => n.contains(el));
  const all = (css) => Array.from(card.querySelectorAll(css)).filter(outside).filter(ok);
  let button = null;
  for (const css of sel.see_more_aria) { button = button || all(css)[0] || null; }
  if (!button) {
    button = all(sel.see_more_candidates).find((el) => textRe.test((el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())) || null;
  }
  if (!button) button = all(sel.legacy_see_more)[0] || null;
  if (button) out.push(button);
}
return out;
}"""
)

SEE_MORE_ARGS: dict[str, Any] = {
    **CARD_ARGS,
    "see_more_aria": list(S.SEE_MORE_ARIA_SELECTORS),
    "see_more_candidates": S.SEE_MORE_TEXT_CANDIDATES,
    "see_more_text_re": S.SEE_MORE_TEXT_RE,
    "see_more_exclude_re": S.SEE_MORE_EXCLUDE_RE,
    "legacy_see_more": S.LEGACY_SEE_MORE,
    "n": 0,
}


def see_more_args(n: int) -> dict[str, Any]:
    return {**SEE_MORE_ARGS, "n": max(0, int(n))}


# --------------------------------------------------------------------------- pure Python
def _clean_line_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def clean_text(value: str) -> str:
    """Normalise whitespace while keeping line structure; drop see-more/less toggles."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    lines = [line.rstrip() for line in value.split("\n")]
    while lines and (not lines[-1].strip() or _TOGGLE_RE.match(lines[-1].strip()) or lines[-1].strip() in ("…", "...")):
        lines.pop()
    text = "\n".join(lines).strip()
    if _TOGGLE_RE.match(text):
        return ""
    return _MANY_BLANK_LINES.sub("\n\n", text)


def urn_from_href(href: Any) -> str | None:
    """``/feed/update/urn:li:activity:N/`` or ``/posts/<slug>-activity-N-xxxx/`` → the post URN."""
    if not isinstance(href, str) or not href:
        return None
    href = unquote(href)
    match = FEED_UPDATE_HREF.search(href)
    if match:
        return match.group(1)
    match = POSTS_SLUG_HREF.search(href)
    if match:
        return f"urn:li:{match.group(1)}:{match.group(2)}"
    return None


def _profile_url(href: Any, base: str) -> str | None:
    if not isinstance(href, str) or not href.strip():
        return None
    parts = urlsplit(urljoin(base + "/", href.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def post_from_raw(raw: dict[str, Any], base: str, today: date) -> ScrapedPost:
    urn = raw.get("urn")
    if not (isinstance(urn, str) and URN_RE.match(urn.strip())):
        urn = urn_from_href(raw.get("post_href"))
    if not isinstance(urn, str) or not URN_RE.match(urn.strip()):
        raise ParseError(f"card has no post URN: {raw.get('urn')!r}")
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


@dataclass(frozen=True)
class ParsedCards:
    posts: list[ScrapedPost]
    parse_errors: int
    cards_found: int
    field_misses: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


def drift_detected(posts_parsed: int, cards_found: int, no_results: bool) -> bool:
    """Layout drift: a logged-in results page with result cards, or without the "no results"
    marker, that yields 0 posts (architecture.md → Layout drift)."""
    return posts_parsed == 0 and (cards_found > 0 or not no_results)


def field_misses_of(posts: list[ScrapedPost]) -> dict[str, int]:
    """Optional field → number of posts missing it; only non-zero entries."""
    misses = {name: sum(1 for p in posts if getattr(p, name) is None) for name in OPTIONAL_POST_FIELDS}
    return {name: n for name, n in misses.items() if n}


def parse_cards(raws: list[dict[str, Any]], base: str, today: date, cap: int | None = None) -> ParsedCards:
    """All raw cards of one page → unique posts (DOM order, URN-deduped, capped) + counters."""
    posts: list[ScrapedPost] = []
    seen: set[str] = set()
    errors: list[str] = []
    for raw in raws:
        if cap is not None and len(posts) >= cap:
            break
        try:
            post = post_from_raw(raw, base, today)
        except ParseError as exc:
            errors.append(str(exc))
            continue
        if post.urn in seen:
            continue  # LinkedIn re-rendered the same card: one post, not an error
        seen.add(post.urn)
        posts.append(post)
    return ParsedCards(
        posts=posts,
        parse_errors=len(errors),
        cards_found=len(raws),
        field_misses=field_misses_of(posts),
        errors=tuple(errors),
    )

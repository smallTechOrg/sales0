"""Layout samples: when a keyword's results could not be read properly, keep a cleaned copy of what the page showed.

A layout warning (``services.runs.keyword_outcome``) says THAT LinkedIn's markup no longer matches ``selectors.py``;
the sample says HOW it changed, so the next selector fix is made from evidence instead of guesses. It is taken on
the page that is still loaded when the keyword's scan ends (no extra page load, no extra LinkedIn request).

What is kept: the outer HTML of up to ``MAX_CARDS`` result containers (the winning card strategy's cards, else the
SDUI list items, else the first children of ``<main>``), with
- scripts, styles, SVG, images, media, iframes and form fields removed;
- only structural attributes kept (``SAMPLE_ATTRS``: ``data-view-name``, ``data-testid``, ``role``, ``componentkey``,
  ``aria-*``, ``href`` without its query string, ``datetime``, ``class`` cut short ...); event handlers and inline
  styles never survive;
- every text node cut to ``MAX_TEXT`` characters and the whole sample to ``MAX_HTML`` characters.
Plus a small fact sheet (page path, title, per-selector counts). No cookies, storage or request data are read.
The sample is UNTRUSTED markup: the app only ever serves it as a download, never renders it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from sales0.logging import get_logger
from sales0.scraper import selectors as S
from sales0.scraper.parsing import CARD_ARGS, CARDS_PRELUDE_JS

log = get_logger("sales0.scraper.layout_sample")

MAX_CARDS = 3
MAX_TEXT = 300
MAX_HTML = 120_000
MAX_CLASS = 120
SAMPLE_ATTRS = ("data-view-name", "data-testid", "role", "componentkey", "href", "datetime", "class", "id", "lang", "dir",
                "data-urn", "data-id", "data-entity-urn", "data-chameleon-result-urn", "type", "tabindex")
DROP_TAGS = ("script", "style", "noscript", "svg", "img", "picture", "video", "audio", "source", "iframe", "canvas",
             "input", "textarea", "select", "link", "meta", "template")
# The fields a keyword outcome warns about (same names and threshold as services.runs.keyword_outcome).
WARN_FIELDS = ("text", "author_name", "author_headline", "posted_relative", "author_profile_url")

SAMPLE_ARGS: dict[str, Any] = {
    **CARD_ARGS,
    "max_cards": MAX_CARDS, "max_text": MAX_TEXT, "max_html": MAX_HTML, "max_class": MAX_CLASS,
    "keep_attrs": list(SAMPLE_ATTRS), "drop_tags": list(DROP_TAGS),
    "probes": {
        "sdui_post": S.SDUI_POST, "sdui_listitem": S.SDUI_LISTITEM, "sdui_actor": S.SDUI_ACTOR,
        "sdui_text": S.join(S.SDUI_TEXT), "card_attr": S.join(S.CARD_ATTR_SELECTORS), "post_links": S.join(S.POST_LINK_SELECTORS),
        "listitem_card": S.LISTITEM_CARD, "role_listitem": '[role="listitem"]', "time": "time", "profile_links": 'a[href*="/in/"]',
    },
}

SAMPLE_JS = (
    "(sel) => {"
    + CARDS_PRELUDE_JS
    + r"""
const picked = __cards(sel);
let strategy = picked.strategy, roots = picked.cards;
const scope = __scope();
if (!roots.length && scope && scope.querySelectorAll) {
  roots = Array.from(scope.querySelectorAll(sel.sdui_listitem));
  strategy = roots.length ? 'listitem_fallback' : 'main';
  if (!roots.length) roots = Array.from((document.querySelector('main') || document.body || document.documentElement).children);
}
const keep = new Set(sel.keep_attrs);
const drop = sel.drop_tags.join(',');
const clean = (root) => {
  const c = root.cloneNode(true);
  c.querySelectorAll(drop).forEach((n) => n.remove());
  const all = [c, ...c.querySelectorAll('*')];
  for (const el of all) {
    for (const a of Array.from(el.attributes)) {
      const n = a.name.toLowerCase();
      if (!(keep.has(n) || n.startsWith('aria-'))) { el.removeAttribute(a.name); continue; }
      if (n === 'href') { const v = a.value || ''; el.setAttribute('href', v.split('?')[0].split('#')[0].slice(0, 300)); }
      else if (n === 'class') { el.setAttribute('class', a.value.slice(0, sel.max_class)); }
      else if (a.value.length > 300) { el.setAttribute(a.name, a.value.slice(0, 300)); }
    }
  }
  const walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_COMMENT);
  const comments = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === 8) { comments.push(n); continue; }
    const t = n.textContent.replace(/\s+/g, ' ');
    n.textContent = t.length > sel.max_text ? t.slice(0, sel.max_text) + '…' : t;
  }
  comments.forEach((n) => n.remove());
  return c.outerHTML;
};
const parts = [];
let size = 0;
for (const r of roots.slice(0, sel.max_cards)) {
  let h = '';
  try { h = clean(r); } catch (e) { h = '<!-- could not copy this card: ' + String(e).slice(0, 100) + ' -->'; }
  if (size + h.length > sel.max_html) { parts.push('<!-- sample cut at ' + sel.max_html + ' characters -->'); break; }
  parts.push(h);
  size += h.length;
}
const counts = {};
for (const [k, s] of Object.entries(sel.probes)) {
  try { counts[k] = document.querySelectorAll(s).length; } catch (e) { counts[k] = -1; }
}
return {
  strategy, html: parts.join('\n\n'), cards_total: roots.length, cards_sampled: Math.min(roots.length, sel.max_cards),
  path: location.pathname.slice(0, 200), title: (document.title || '').slice(0, 200), counts,
};
}"""
)


def needs_sample(*, posts: int, parse_errors: int, drift: bool, field_misses: Mapping[str, int] | None) -> bool:
    """True when the keyword will end with a layout warning or unreadable cards: drift, any card that could not be
    parsed, or a watched field missing on at least half of the posts (``keyword_outcome``'s rule)."""
    if drift or parse_errors > 0:
        return True
    for name, count in (field_misses or {}).items():
        if name not in WARN_FIELDS or int(count) <= 0:
            continue
        total = posts + int(count) if name == "text" else posts
        if total >= 2 and min(int(count), total) * 2 >= total:
            return True
    return False


def capture(page: Page, *, reason: str) -> dict[str, Any] | None:
    """The cleaned sample of the loaded page, or None (never raises: a sample must not cost the keyword)."""
    try:
        data = page.evaluate(SAMPLE_JS, SAMPLE_ARGS)
    except PlaywrightError as exc:
        log.warning("layout_sample_failed", error=str(exc)[:200])
        return None
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        log.warning("layout_sample_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return None
    if not isinstance(data, dict):
        return None
    sample = {
        "reason": reason[:200],
        "strategy": str(data.get("strategy") or "none")[:40],
        "html": str(data.get("html") or "")[:MAX_HTML + 200],
        "cards_total": int(data.get("cards_total") or 0),
        "cards_sampled": int(data.get("cards_sampled") or 0),
        "path": str(data.get("path") or "")[:200],
        "title": str(data.get("title") or "")[:200],
        "counts": {str(k)[:40]: int(v) for k, v in (data.get("counts") or {}).items() if isinstance(v, int | float)},
    }
    log.info("layout_sample_captured", reason=sample["reason"], strategy=sample["strategy"], bytes=len(sample["html"]),
             cards=sample["cards_total"])
    return sample


def reason_of(*, posts: int, parse_errors: int, drift: bool, field_misses: Mapping[str, int] | None) -> str:
    bits = []
    if drift:
        bits.append("no posts parsed")
    if parse_errors:
        bits.append(f"{parse_errors} card(s) unreadable")
    misses = {k: int(v) for k, v in (field_misses or {}).items() if int(v) > 0 and k in WARN_FIELDS}
    if misses:
        bits.append("missing " + ", ".join(f"{k}={v}" for k, v in sorted(misses.items())))
    return f"{posts} posts parsed; " + ("; ".join(bits) or "layout warning")


__all__ = ["MAX_HTML", "SAMPLE_ARGS", "SAMPLE_JS", "capture", "needs_sample", "reason_of"]

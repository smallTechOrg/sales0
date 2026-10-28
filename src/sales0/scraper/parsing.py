"""The ONE post parser, shared by live and fixture mode (architecture.md → Post parsing and
"## LinkedIn SDUI search results").

``EXTRACT_JS`` discovers the cards itself (Phase 1.2: SDUI posts first; else the URN layout:
URN attributes, then post links) and pulls every field through ordered fallbacks from ``selectors.py``; class-based selectors survive only as
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
from urllib.parse import urljoin, urlsplit, urlunsplit

from sales0.scraper import selectors as S
from sales0.scraper.relative_time import normalize_relative, parse_relative
from sales0.scraper.types import OPTIONAL_POST_FIELDS, TEXT_FIELD, TRUNCATED_FIELD, CardStrategy, ScrapedPost
from sales0.scraper.urls import (  # urn_from_href is re-exported (it lives in urls since Phase 1.2)
    FEED_UPDATE_HREF,
    POSTS_SLUG_HREF,
    canonical_post_url,
    link_kind_of,
    permalink,
    urn_from_href,
)

__all__ = [
    "CARDS_PRELUDE_JS", "CARD_ARGS", "COUNT_CARDS_JS", "EXTRACT_ARGS", "EXTRACT_JS", "FEED_UPDATE_HREF",
    "POSTS_SLUG_HREF", "SEE_MORE_ARGS", "SEE_MORE_JS", "URN_RE", "ParseError", "ParsedCards", "TextMissing",
    "clean_text", "component_key_of", "drift_detected", "field_misses_of", "parse_cards", "post_from_raw",
    "see_more_args", "source_ref_of", "strategy_of", "urn_from_href",
]

URN_RE = re.compile(r"^urn:li:(?:activity|ugcPost|share):\d+$")
_TOGGLE_RE = re.compile(r"^(…|\.\.\.)?\s*((see|show)\s+)?(more|less)$", re.IGNORECASE)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
_INLINE_TOGGLE_RE = re.compile(r"\s*(…|\.\.\.)\s*((see|show)\s+)?more$", re.IGNORECASE)


class ParseError(ValueError):
    """One result card could not be turned into a post."""


class TextMissing(ParseError):
    """Phase 1.2: a result container with a ref but no readable post text (counted as a field miss)."""


# --------------------------------------------------------------------------- JS
# Shared in-page helpers. ``sel`` is CARD_ARGS (or a superset of it).
# ONE ordered card strategy (architecture.md → "Card discovery"): ``sdui`` (every outermost
# SDUI_POST), else ``urn`` (Phase 1.1: URN attributes, then post links), else ``none``.
# ``__cardsFound`` is "cards found" everywhere: the winning strategy's cards, or — when
# neither found anything — the number of SDUI listitems (so "listitems but no posts" is drift).
CARDS_PRELUDE_JS = r"""
const __scope = () => document.querySelector('main') || document;
const __domOrder = (a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
const __urnCards = (sel, scope) => {
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
  cards.sort(__domOrder);
  return cards;
};
const __listCards = (sel, scope) => Array.from(scope.querySelectorAll(sel.listitem_card))
  .filter((li) => !(li.parentElement && li.parentElement.closest(sel.listitem_card))
    && sel.listitem_markers.some((m) => li.querySelector(m)));
const __cards = (sel) => {
  const scope = __scope();
  if (!scope || !scope.querySelectorAll) return { strategy: 'none', cards: [] };
  const sdui = Array.from(scope.querySelectorAll(sel.sdui_post))
    .filter((el) => !(el.parentElement && el.parentElement.closest(sel.sdui_post)));
  if (sdui.length) return { strategy: 'sdui', cards: sdui };
  const urn = __urnCards(sel, scope);
  if (urn.length) return { strategy: 'urn', cards: urn };
  const lst = __listCards(sel, scope);
  if (lst.length) return { strategy: 'listitem', cards: lst };
  return { strategy: 'none', cards: [] };
};
const __cardsFound = (sel) => {
  const r = __cards(sel);
  if (r.strategy !== 'none') return r.cards.length;
  const scope = __scope();
  return scope && scope.querySelectorAll ? scope.querySelectorAll(sel.sdui_listitem).length : 0;
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
    "sdui_post": S.SDUI_POST,
    "sdui_listitem": S.SDUI_LISTITEM,
    "listitem_card": S.LISTITEM_CARD,
    "listitem_markers": list(S.LISTITEM_POST_MARKERS),
    "card_attr": S.join(S.CARD_ATTR_SELECTORS),
    "post_links": S.join(S.POST_LINK_SELECTORS),
    "containers": S.CARD_CONTAINERS,
    "legacy_no_results": S.LEGACY_NO_RESULTS,
    "no_results_tags": S.NO_RESULTS_TEXT_TAGS,
    "no_results_prefix": S.NO_RESULTS_PREFIX,
    "no_results_exact": S.NO_RESULTS_EXACT,
}

# Number of cards currently discovered (wait-for-more-cards polling); same meaning as SIGNALS_JS.cards_found.
COUNT_CARDS_JS = "(sel) => {" + CARDS_PRELUDE_JS + "return __cardsFound(sel); }"

# Per-card field extraction. Returns one raw dict per discovered card (DOM order):
# {strategy, urn, component_key, post_href, author_name, author_profile_url, author_headline,
#  posted_relative, text, has_control_menu, via} (contract S2 → S3; architecture.md → "SDUI field
# extraction"). The URN strategy fills component_key=null, has_control_menu=false.
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
  const head = t.replace(/^\s*edited\s*[•·]\s*/i, '').split(/[•·]/)[0].trim();
  return /^(just )?now$/i.test(head) || relShort.test(head) || relLong.test(head);
};
const skipRe = new RegExp(sel.headline_skip_re, 'i');
const feedRe = new RegExp(sel.feed_re);
const postsRe = new RegExp(sel.posts_re);
const anyUrnRe = new RegExp(sel.any_urn_re);
const keyRe = new RegExp(sel.listitem_key_re);
const decode = (h) => { try { return decodeURIComponent(h); } catch (e) { return h; } };
const isSkip = (l) => skipRe.test(l) || isRel(l) || /^[•·|\-\s]+$/.test(l) || (/^view /i.test(l) && /profile$/i.test(l));
const degreeTail = /\s*[•·]\s*(?:1st|2nd|3rd\+?|\d+(?:st|nd|rd|th)\+?)\s*$/i;

// ---------------------------------------------------------------- URN layout (Phase 1.1)
const urnRaw = (card, idx) => {
  const nested = Array.from(card.querySelectorAll(sel.card_attr));
  const outside = (el) => !nested.some((n) => n.contains(el));
  const all = (css) => Array.from(card.querySelectorAll(css)).filter(outside);
  const first = (css) => all(css)[0] || null;
  const via = { strategy: 'urn' };

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
  const truncated = !!text && truncatedOf(textEl, text);

  // Headline: first line of the author anchor (or its parent) that is not name/degree/Follow/time.
  const skip = (l) => (name && l.includes(name)) || isSkip(l);
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

  return { strategy: 'urn', urn, component_key: null, post_href: postHref, author_name: name, author_profile_url: profile,
           author_headline: headline, posted_relative: posted, text, has_control_menu: false,
           card_index: idx, menu_label: null, truncated, via };
};

// ---------------------------------------------------------------- SDUI (Phase 1.2)
const textCss = sel.sdui_text.join(', ');
const exclCss = sel.sdui_text_exclude.join(', ');
// innerText of a NOT-rendered element falls back to its full textContent, so hidden text (a `hidden`
// or display:none element or ancestor) is checked explicitly. display:contents wrappers stay readable.
const isHidden = (el) => {
  for (let x = el; x && x.nodeType === 1; x = x.parentElement) {
    if (x.hidden || getComputedStyle(x).display === 'none') return true;
  }
  return false;
};
const shown = (el) => (!el || isHidden(el) ? '' : (el.innerText !== undefined ? el.innerText : (el.textContent || '')));
// Audit #7: the text is still cut short when an unexpanded "…more" toggle sits with it (hidden or
// already-expanded toggles do not count) or the text itself ends in an ellipsis marker.
const truncatedOf = (textEl, rawText) => {
  if (!textEl) return false;
  const expand = textEl.querySelector(sel.sdui_expand) || (textEl.parentElement ? textEl.parentElement.querySelector(sel.sdui_expand) : null);
  if (expand && !isHidden(expand) && expand.getAttribute('aria-expanded') !== 'true') return true;
  return /(…|\.\.\.)\s*((see|show)\s+)?(more)?$/i.test((rawText || '').replace(/\s+$/, ''));
};
// Phase 1.4: contact links whose address is not in the visible text (a mailto:/tel: anchor labelled
// "email me" ...) are appended, so the e-mail/phone the poster published is never lost.
const contactTail = (el, text) => {
  if (!el) return '';
  const norm = (v) => v.toLowerCase().replace(/[\s()-]+/g, '');
  const hay = norm(text || '');
  const extra = [];
  for (const a of el.querySelectorAll('a[href^="mailto:" i], a[href^="tel:" i]')) {
    const h = decode(a.getAttribute('href') || '').replace(/^(mailto:|tel:)/i, '').split('?')[0].trim();
    if (h && !hay.includes(norm(h)) && !extra.includes(h)) extra.push(h);
  }
  // Audit #12: the target of an external link whose (possibly truncated) label does not show it.
  // LinkedIn-internal links (hashtags, mentions, "#") are not contact data and are skipped.
  for (const a of el.querySelectorAll('a[href]')) {
    const raw = (a.getAttribute('href') || '').trim();
    if (!/^https?:\/\//i.test(raw)) continue;
    let host = '';
    try { host = new URL(raw).hostname.toLowerCase(); } catch (e) { continue; }
    if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) continue;
    const bare = norm(raw.replace(/^https?:\/\//i, '').replace(/\/$/, ''));
    if (hay.includes(bare) || extra.includes(raw)) continue;
    extra.push(raw);
  }
  return extra.length ? '\n' + extra.join('\n') : '';
};
const validKey = (v) => {
  const t = typeof v === 'string' ? v.trim() : '';
  return t && !/\s/.test(t) && t.length <= sel.key_max_len ? t : null;
};

const sduiRaw = (card, idx) => {
  const via = { strategy: 'sdui' };
  const nestedPosts = Array.from(card.querySelectorAll(sel.sdui_post));
  const inNested = (el) => nestedPosts.some((n) => n.contains(el));
  const within = (el, css) => { const x = el.closest(css); return !!x && card.contains(x) && x !== card; };
  const usable = (el) => !inNested(el) && !within(el, exclCss);

  // component_key: KEY. The inner div[componentkey="<KEY>"] carries the same value as the listitem's
  // "expanded<KEY>FeedType_…"; when the listitem KEY is readable it is used (so a sub-component's
  // own componentkey — an actor's or a button's — can never become the post's identity), else the
  // first descendant-or-self componentkey that is not in the listitem form.
  const li = card.closest(sel.sdui_listitem);
  const liMatch = li ? (li.getAttribute(sel.key_attr) || '').trim().match(keyRe) : null;
  const liKey = liMatch ? validKey(liMatch[1]) : null;
  const keyed = [card, ...card.querySelectorAll('[' + sel.key_attr + ']')].filter((el) => el.hasAttribute(sel.key_attr) && !inNested(el));
  const inner = keyed.map((el) => (el.getAttribute(sel.key_attr) || '').trim()).filter((v) => v && !keyRe.test(v));
  let key = null;
  if (liKey) { key = liKey; via.component_key = inner.includes(liKey) ? 'inner' : 'listitem'; }
  else if (inner.length && validKey(inner[0])) { key = validKey(inner[0]); via.component_key = 'inner'; }

  // URN (best effort; absent on current LinkedIn): post links, URN attributes, any attribute.
  let urn = null, postHref = null;
  const hrefs = Array.from(card.querySelectorAll('a[href]')).filter((a) => !inNested(a)).map((a) => decode(a.getAttribute('href') || ''));
  for (const h of hrefs) { const m = h.match(feedRe); if (m) { postHref = h; urn = m[1]; via.urn = 'feed_update_href'; break; } }
  if (!urn) { for (const h of hrefs) { const m = h.match(postsRe); if (m) { postHref = h; urn = 'urn:li:' + m[1] + ':' + m[2]; via.urn = 'posts_href'; break; } } }
  const elements = [card, ...card.querySelectorAll('*')].filter((el) => !inNested(el));
  if (!urn) {
    for (const el of elements) {
      for (const attr of sel.urn_attributes) { urn = urn || validUrn(el.getAttribute(attr)); }
      if (urn) { via.urn = 'attr'; break; }
    }
  }
  if (!urn) {
    outer: for (const el of elements) {
      for (const at of Array.from(el.attributes)) {
        const m = decode(at.value || '').match(anyUrnRe);
        if (m) { urn = m[0]; via.urn = 'any_attr'; break outer; }
      }
    }
  }

  // Actor lines: every SDUI_ACTOR (DOM order) outside the text, excluded media and nested posts.
  const actors = Array.from(card.querySelectorAll(sel.sdui_actor)).filter((a) => usable(a) && !within(a, textCss));
  const collapse = (ls) => ls.filter((l, i) => i === 0 || ls[i - 1] !== l);
  const okBlock = (el) => el && el !== card && card.contains(el) && !el.matches(textCss) && !el.querySelector(textCss)
    && !el.querySelector(exclCss) && !el.querySelector(sel.sdui_control_menu) && !el.querySelector(sel.sdui_post)
    && !el.querySelector('h1, h2, h3');
  // The actor block: on current LinkedIn the headline and time sit beside the actor links, inside
  // the smallest element holding every actor (two actors) or the actor's widest text-free ancestor (one).
  let block = null;
  if (actors.length >= 2) {
    let c = actors[0];
    while (c && !actors.every((a) => c.contains(a))) c = c.parentElement;
    if (okBlock(c)) block = c;
    // Audit #2: the common ancestor swallowed text/media/menu: fall back to the widest text-free
    // ancestor of any single actor that holds the relative time.
    if (!block) {
      for (const a0 of actors) {
        let d = a0;
        while (d.parentElement && okBlock(d.parentElement)) d = d.parentElement;
        if (d !== a0 && lines(d).some(isRel)) { block = d; break; }
      }
    }
  } else if (actors.length === 1) {
    let c = actors[0];
    while (c.parentElement && okBlock(c.parentElement)) c = c.parentElement;
    if (c !== actors[0]) block = c;
  }
  const actorLines = collapse(actors.flatMap((a) => lines(a)));
  const ls = block ? collapse(lines(block)) : actorLines;
  via.actor_lines = block ? 'actor_block' : 'actors';

  // Author anchor: the first profile/company link inside (or being) an actor.
  let anchor = null;
  for (const a of actors) {
    anchor = a.matches(sel.author_anchor) ? a : a.querySelector(sel.author_anchor);
    if (anchor) break;
  }
  const profile = anchor ? (anchor.getAttribute('href') || null) : null;
  if (profile) via.author_profile_url = 'actor_anchor';

  // author_name: the first actor line that is not a degree/Follow/Premium/"View … profile"/time line
  // (a trailing inline "• 3rd+" is cut); else the actor anchor's aria-label, cleaned.
  let name = null;
  let nameAt = ls.findIndex((l) => !isSkip(l) && clean(l.replace(degreeTail, '')));
  if (nameAt >= 0) { name = clean(ls[nameAt].replace(degreeTail, '')); via.author_name = 'actor_line'; }
  if (!name) {
    for (const a of actors) {
      const holder = a.matches('[aria-label]') ? a : (a.querySelector(sel.author_anchor + ', [aria-label]'));
      let label = holder ? (holder.getAttribute('aria-label') || '') : '';
      for (const s of sel.aria_strip) label = label.split(s).join(' ');
      label = clean(label);
      if (label && !isRel(label)) { name = label; via.author_name = 'aria-label'; break; }
    }
  }

  // author_headline: the next such line that does not contain the name.
  let headline = null;
  if (name) {
    headline = ls.slice(nameAt + 1).find((l) => !isSkip(l) && !l.includes(name)) || null;
    if (headline) via.author_headline = via.actor_lines;
  }

  // posted_relative: the first actor line (or aria-label) whose head is a relative time.
  let posted = ls.find((l) => isRel(l)) || null;
  if (posted) via.posted_relative = via.actor_lines;
  if (!posted) {
    const scopeEls = block ? [block, ...block.querySelectorAll('[aria-label]')] : actors.flatMap((a) => [a, ...a.querySelectorAll('[aria-label]')]);
    for (const el of scopeEls) {
      const label = clean(el.getAttribute('aria-label'));
      if (label && isRel(label)) { posted = label; via.posted_relative = 'aria-label'; break; }
    }
  }
  if (!posted) {
    const timeEl = Array.from(card.querySelectorAll('time')).find(usable);
    if (timeEl) { posted = clean(timeEl.innerText || timeEl.textContent) || clean(timeEl.getAttribute('datetime')) || null; if (posted) via.posted_relative = 'time'; }
  }

  // text: the first SDUI_TEXT match (selector order) with non-empty text outside SDUI_TEXT_EXCLUDE,
  // minus a trailing "…more" (the SDUI_EXPAND element's own text) and a trailing "…".
  let textEl = null;
  for (const css of sel.sdui_text) {
    textEl = Array.from(card.querySelectorAll(css)).find((el) => usable(el) && clean(shown(el))) || null;
    if (textEl) { via.text = css; break; }
  }
  if (!textEl) {
    // Phase 1.1 fallback (unchanged rule): the LONGEST text candidate, never inside the actors, the
    // actor block, excluded media, the control menu or a nested post.
    let best = -1;
    for (const css of sel.text_selectors) {
      for (const el of card.querySelectorAll(css)) {
        if (!usable(el) || within(el, sel.sdui_control_menu) || within(el, sel.sdui_actor)) continue;
        if ((block && (block.contains(el) || el.contains(block))) || actors.some((a) => el.contains(a))) continue;
        const len = clean(shown(el)).length;
        if (len > best && len > 0) { textEl = el; best = len; via.text = 'fallback:' + css; }
      }
    }
  }
  let text = null, truncated = false;
  if (textEl) {
    text = shown(textEl);
    truncated = truncatedOf(textEl, text);
    const expand = textEl.querySelector(sel.sdui_expand) || (textEl.parentElement ? textEl.parentElement.querySelector(sel.sdui_expand) : null);
    text = text.replace(/\s+$/, '');
    if (expand) {
      const raw = (expand.innerText || expand.textContent || '').trim();
      for (const tail of [raw, clean(raw)]) {
        if (tail && text.endsWith(tail)) { text = text.slice(0, -tail.length).replace(/\s+$/, ''); break; }
      }
    }
    text = text.replace(/\s*(…|\.\.\.)$/, '');
    if (!clean(text)) { text = null; truncated = false; delete via.text; }
    else text += contactTail(textEl, text);
  }
  // A reshare: the reshared original's text is the only text when the resharer wrote none; when
  // they did, the original follows it, marked (audit #13), so its e-mail/phone/ask is not lost.
  if (nestedPosts.length) {
    for (const n of nestedPosts) {
      const el = Array.from(n.querySelectorAll(textCss)).find((e) => clean(shown(e)) && !e.closest(exclCss));
      if (!el) continue;
      const rawNested = shown(el);
      let t = rawNested.replace(/\s+$/, '').replace(/\s*(…|\.\.\.)\s*((see|show)\s+)?more$/i, '').replace(/\s*(…|\.\.\.)$/, '');
      if (!clean(t)) continue;
      const nestedTruncated = truncatedOf(el, rawNested);
      if (!text) { text = t + contactTail(el, t); via.text = 'reshared_original'; truncated = nestedTruncated; }
      else { text += '\n\n[Reshared post] ' + t + contactTail(el, t); via.reshared = 'appended'; truncated = truncated || nestedTruncated; }
      break;
    }
  }

  return { strategy: 'sdui', urn, component_key: key, post_href: postHref, author_name: name, author_profile_url: profile,
           author_headline: headline, posted_relative: posted, text, has_control_menu: !!card.querySelector(sel.sdui_control_menu),
           truncated, card_index: idx, menu_label: (() => { const b = card.querySelector(sel.sdui_control_menu + ' button, ' + sel.listitem_menu); return b ? (clean(b.getAttribute('aria-label')) || null) : null; })(),
           via };
};

// ---------------------------------------------------------------- listitem variant (Phase 1.3)
// No data-view-name needed: name from the "Open control menu for post by <Name>" label, text from the
// expandable text box, headline/time from the text nodes BEFORE the text. Phase 1.4: carries
// has_control_menu / card_index / menu_label so permalink capture works on this tier too.
const listRaw = (card, idx) => {
  const via = { strategy: 'listitem' };
  const menu = card.querySelector(sel.listitem_menu);
  const label = menu ? clean(menu.getAttribute('aria-label')) : '';
  const lm = label.match(new RegExp(sel.listitem_menu_re, 'i'));
  let name = lm ? clean(lm[1]) : null;
  if (name) via.author_name = 'control_menu_label';

  const anchor = Array.from(card.querySelectorAll(sel.author_anchor))[0] || null;
  const profile = anchor ? (anchor.getAttribute('href') || null) : null;
  if (profile) via.author_profile_url = 'first_profile_link';
  if (!name && anchor) {
    const holder = anchor.matches('[aria-label]') ? anchor : anchor.querySelector('[aria-label]');
    let l = holder ? (holder.getAttribute('aria-label') || '') : '';
    for (const s of sel.aria_strip) l = l.split(s).join(' ');
    l = clean(l.replace(degreeTail, ''));
    if (l) { name = l; via.author_name = 'anchor_label'; }
  }

  let textEl = null, best = 0;
  for (const css of sel.listitem_text) {
    for (const el of card.querySelectorAll(css)) {
      if (exclCss && el.closest(exclCss)) continue;  // article titles, document captions, image alt text
      const len = clean(shown(el)).length;
      if (len > best) { textEl = el; best = len; via.text = css; }
    }
  }
  let text = null, truncated = false;
  if (textEl) {
    text = shown(textEl).replace(/\s+$/, '');
    truncated = truncatedOf(textEl, text);
    const expand = textEl.querySelector(sel.sdui_expand) || (textEl.parentElement ? textEl.parentElement.querySelector(sel.sdui_expand) : null);
    if (expand) {
      const raw = (expand.innerText || expand.textContent || '').trim();
      for (const tail of [raw, clean(raw)]) {
        if (tail && text.endsWith(tail)) { text = text.slice(0, -tail.length).replace(/\s+$/, ''); break; }
      }
    }
    text = text.replace(/\s*(…|\.\.\.)$/, '');
    if (!clean(text)) { text = null; truncated = false; delete via.text; }
    else text += contactTail(textEl, text);
  }

  // Text nodes before the post text, in DOM order: headline and relative time.
  const head = [];
  const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el) continue;
    if (textEl && (textEl.contains(el) || (textEl.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))) break;
    if (el.closest('button, h1, h2, h3, script, style') || isHidden(el)) continue;
    const t = clean(n.textContent);
    if (t && t !== '​') head.push(t);
  }
  const skipLine = (l) => isSkip(l) || /^feed post$/i.test(l) || (name && l === name);
  if (!name) {
    const first = head.find((l) => !skipLine(l));
    if (first) { name = clean(first.replace(degreeTail, '')); via.author_name = 'first_line'; }
  }
  const posted = head.find((l) => isRel(l)) || null;
  if (posted) via.posted_relative = 'head_lines';
  const headline = head.find((l) => !skipLine(l) && !(name && l.includes(name))) || null;
  if (headline) via.author_headline = 'head_lines';

  const rawKey = (card.getAttribute(sel.key_attr) || '').trim().replace(new RegExp(sel.listitem_key_prefix_re), '');
  const key = validKey(rawKey) || (() => {
    const inner = Array.from(card.querySelectorAll('[' + sel.key_attr + ']')).map((e) => (e.getAttribute(sel.key_attr) || '').trim());
    return validKey(inner[0]);
  })();
  if (key) via.component_key = 'listitem';
  return { strategy: 'listitem', urn: null, component_key: key, post_href: null, author_name: name,
           author_profile_url: profile, author_headline: headline, posted_relative: posted, text,
           has_control_menu: !!menu, card_index: idx, menu_label: label || null, truncated, via };
};

const found = __cards(sel);
let raws = found.cards.map((card, i) => (found.strategy === 'sdui' ? sduiRaw(card, i) : found.strategy === 'listitem' ? listRaw(card, i) : urnRaw(card, i)));
// A variant that matched cards but gave no text on any of them: try the marker-free listitem tier.
if (found.strategy !== 'listitem' && !raws.some((r) => r && r.text)) {
  const alt = __listCards(sel, __scope()).map((c, i) => listRaw(c, i));
  if (alt.some((r) => r.text)) raws = alt;
}
return raws;
}"""
)

EXTRACT_ARGS: dict[str, Any] = {
    **CARD_ARGS,
    "urn_attributes": list(S.URN_ATTRIBUTES),
    "urn_prefixes": list(S.URN_PREFIXES),
    "feed_re": S.FEED_UPDATE_HREF_RE,
    "posts_re": S.POSTS_SLUG_HREF_RE,
    "any_urn_re": S.ANY_URN_RE,
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
    # Phase 1.2 (SDUI)
    "listitem_key_re": S.SDUI_LISTITEM_KEY_RE,
    "key_attr": S.SDUI_KEY_ATTR,
    "key_max_len": S.SDUI_KEY_MAX_LEN,
    "sdui_actor": S.SDUI_ACTOR,
    "sdui_text": list(S.SDUI_TEXT),
    "sdui_text_exclude": list(S.SDUI_TEXT_EXCLUDE),
    "sdui_expand": S.SDUI_EXPAND,
    "sdui_control_menu": S.SDUI_CONTROL_MENU,
    # Phase 1.3 (listitem variant)
    "listitem_text": list(S.LISTITEM_TEXT),
    "listitem_menu": S.LISTITEM_MENU,
    "listitem_menu_re": S.LISTITEM_MENU_LABEL_RE,
    "listitem_key_prefix_re": S.LISTITEM_KEY_PREFIX_RE,
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
for (const card of __cards(sel).cards.slice(0, sel.n)) {
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
    text = _INLINE_TOGGLE_RE.sub("", "\n".join(lines)).strip()  # SDUI: "…more" inline at the end
    if _TOGGLE_RE.match(text):
        return ""
    return _MANY_BLANK_LINES.sub("\n\n", text)


def _profile_url(href: Any, base: str) -> str | None:
    if not isinstance(href, str) or not href.strip():
        return None
    parts = urlsplit(urljoin(base + "/", href.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def component_key_of(value: Any) -> str | None:
    """A usable SDUI KEY: trimmed, non-empty, no whitespace, <= 197 chars (``ck:<KEY>`` fits String(200))."""
    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key or any(ch.isspace() for ch in key) or len(key) > S.SDUI_KEY_MAX_LEN:
        return None
    return key


def _valid_urn(value: Any) -> str | None:
    if isinstance(value, str) and URN_RE.match(value.strip()):
        return value.strip()
    return None


def source_ref_of(raw: dict[str, Any], base: str) -> str | None:
    """The post's identity: its URN (own, from the post link, from the copied link), else ``ck:<KEY>``.

    The copied "Copy link to post" URL counts only when it is a same-site link
    (``canonical_post_url``), so a foreign URL can never become a post identity.
    """
    copied_urn, _ = canonical_post_url(raw.get("copied_url"), base)
    urn = _valid_urn(raw.get("urn")) or urn_from_href(raw.get("post_href")) or copied_urn
    if urn:
        return urn
    key = component_key_of(raw.get("component_key"))
    return f"{S.SOURCE_REF_KEY_PREFIX}{key}" if key else None


def post_from_raw(raw: dict[str, Any], base: str, today: date, search_url: str | None = None) -> ScrapedPost:
    """One raw card → a ``ScrapedPost`` (pure).

    Raises ``ParseError`` when the card has no source ref or no link at all, and
    ``TextMissing`` (a ``ParseError``) when it has a ref but no readable text.
    """
    ref = source_ref_of(raw, base)
    if ref is None:
        raise ParseError(f"card has no post URN or component key: {raw.get('urn')!r}")
    text_raw = raw.get("text")
    text = clean_text(text_raw) if isinstance(text_raw, str) else ""
    if not text:
        raise TextMissing(f"card {ref} has no post text")
    profile = _profile_url(raw.get("author_profile_url"), base)
    urn = ref if ref.startswith("urn:li:") else None
    _, copied_post_url = canonical_post_url(raw.get("copied_url"), base)
    if urn is not None:
        url = permalink(base, urn)
    elif copied_post_url is not None:
        url = copied_post_url
    elif profile is not None:
        url = profile
    elif isinstance(search_url, str) and search_url.strip():
        url = search_url.strip()
    else:
        raise ParseError(f"card {ref} has no post, author or search link")
    relative = normalize_relative(_clean_line_value(raw.get("posted_relative")))
    return ScrapedPost(
        source_ref=ref,
        url=url,
        author_name=_clean_line_value(raw.get("author_name")),
        author_profile_url=profile,
        author_headline=_clean_line_value(raw.get("author_headline")),
        posted_relative=relative,
        posted_date=parse_relative(relative, today),
        text=text,
        link_kind=link_kind_of(url) or "other",
        search_url=search_url,
        truncated=raw.get("truncated") is True,
    )


@dataclass(frozen=True)
class ParsedCards:
    posts: list[ScrapedPost]
    parse_errors: int
    cards_found: int
    field_misses: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    strategy: CardStrategy = "none"  # Phase 1.2: which card strategy produced the raws


def drift_detected(posts_parsed: int, cards_found: int, no_results: bool) -> bool:
    """Layout drift: a logged-in results page with result cards, or without the "no results"
    marker, that yields 0 posts (architecture.md → Layout drift)."""
    return posts_parsed == 0 and (cards_found > 0 or not no_results)


def field_misses_of(posts: list[ScrapedPost]) -> dict[str, int]:
    """Optional field → number of posts missing it; only non-zero entries."""
    misses = {name: sum(1 for p in posts if getattr(p, name) is None) for name in OPTIONAL_POST_FIELDS}
    return {name: n for name, n in misses.items() if n}


def strategy_of(raws: list[dict[str, Any]]) -> CardStrategy:
    """The page's card strategy as reported by EXTRACT_JS (raws without one are URN-layout cards)."""
    if not raws:
        return "none"
    value = raws[0].get("strategy")
    return value if value in ("sdui", "urn", "listitem") else "urn"


def parse_cards(
    raws: list[dict[str, Any]], base: str, today: date, cap: int | None = None, search_url: str | None = None
) -> ParsedCards:
    """All raw cards of one page → unique posts (DOM order, deduped by source ref, capped) + counters.

    A container with a ref but no readable text is not a post: it is counted in
    ``field_misses["text"]`` (Phase 1.2), not as a parse error.
    """
    posts: list[ScrapedPost] = []
    seen: set[str] = set()
    errors: list[str] = []
    textless: set[str | None] = set()
    for raw in raws:
        if cap is not None and len(posts) >= cap:
            break
        try:
            post = post_from_raw(raw, base, today, search_url=search_url)
        except TextMissing:
            textless.add(source_ref_of(raw, base))  # one miss per container, however often it re-renders
            continue
        except ParseError as exc:
            errors.append(str(exc))
            continue
        if post.source_ref in seen:
            continue  # LinkedIn re-rendered the same card: one post, not an error
        seen.add(post.source_ref)
        posts.append(post)
    text_misses = len(textless - seen)
    misses = {TEXT_FIELD: text_misses} if text_misses else {}
    misses.update(field_misses_of(posts))
    truncated = sum(1 for p in posts if p.truncated)
    if truncated:
        misses[TRUNCATED_FIELD] = truncated
    return ParsedCards(
        posts=posts,
        parse_errors=len(errors),
        cards_found=len(raws),
        field_misses=misses,
        errors=tuple(errors),
        strategy=strategy_of(raws),
    )

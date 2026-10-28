"""Post permalinks via the post's "…" menu → "Copy link to post" (architecture.md →
"## LinkedIn SDUI search results" → Permalink capture).

LinkedIn's SDUI search results carry no post id and no post link. For posts without one,
sales0 opens the post's control menu and presses "Copy link to post", then reads the URL the
page tried to copy. The copy is intercepted INSIDE the page: ``CLIPBOARD_HOOK_JS`` replaces
``navigator.clipboard`` with a recorder that never calls the original, so the computer's
clipboard is never read or written.

Runs per keyword AFTER "see more" expansion and AFTER ``EXTRACT_JS`` (so it can never change
what was parsed); it only adds ``raw["copied_url"]`` on a hit. Safety:

- Only the menu trigger and the item whose trimmed text is exactly "Copy link to post" are
  ever clicked; the menu is closed with Escape.
- Every click is paced (``pacer.delay`` before the trigger, ``pacer.short`` before the item
  and before Escape) and followed by a safety ``checkpoint`` (a stop propagates).
- Phase 1.4: both card tiers (``sdui`` and ``listitem``); a card no longer in the DOM ("no_card") is
  skipped and counts neither as an attempt nor a miss; ``eligible_refs`` picks cards by ref.
- At most ``cap`` attempts per keyword (0 = no hook, no clicks); 3 consecutive misses end
  capture for the keyword; leaving the search page ends it too.

Audit fixes (AUDIT_SCRAPER.md): #1 the SDUI tier trusts a post only by its component key, or - as a
last resort - by ``card_index`` when the trigger's aria-label AND the post's leading text match what
was extracted (a raw from an earlier pass has no ``card_index`` and is found by key only: the wrong
post's URL must never be attached); #9 the SDUI trigger must carry an aria-label like "control menu"
/ "more actions"; #10 an unresolved ``lnkd.in`` short link is a MISS (never stored), resolved through
the account proxy with the browser's own User-Agent inside one short shared budget; #14 the waits
around a menu are randomised, not fixed.

Known risk (documented, not removed): ``CLIPBOARD_HOOK_JS`` replaces ``navigator.clipboard`` inside
the page; a page script that fingerprints the property descriptor can see it. It is installed only
when a capture actually runs (never on the plain scan) and only to avoid touching the user's clipboard.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from playwright.sync_api import ElementHandle, Page
from playwright.sync_api import Error as PlaywrightError

from sales0.logging import get_logger
from sales0.scraper import selectors as S
from sales0.scraper.pacing import Pacer
from sales0.scraper.parsing import URN_RE, urn_from_href

log = get_logger("sales0.scraper.permalink")
_DEFAULT_PACER = Pacer(3.0, 9.0)

Outcome = Literal["hit", "no_menu_item", "no_copy", "click_error", "no_card", "unresolved"]

MAX_CONSECUTIVE_MISSES = 3
MENU_ITEM_TIMEOUT_MS = 2_000  # wait for "Copy link to post" to become visible
TAKE_POLLS = 15  # poll the captured list this many times …
TAKE_POLL_MS = 100  # … this far apart (≈ 1.5 s)
CLICK_TIMEOUT_MS = 3_000
_TRIGGER = 'button, [role="button"]'
_DOC_MENU = '[role="menu"]'

# Idempotent. Replaces navigator.clipboard (even where it is undefined) with a recorder whose
# writeText(t) pushes String(t) and resolves, and whose write() resolves without effect.
# The original clipboard object is never read, kept or called.
CLIPBOARD_HOOK_JS = r"""() => {
  if (!Array.isArray(window.__sales0_copied)) window.__sales0_copied = [];
  if (window.__sales0_clipboard_hooked) return true;
  const recorder = {
    writeText: (t) => { window.__sales0_copied.push(String(t)); return Promise.resolve(); },
    write: () => Promise.resolve(),
    readText: () => Promise.resolve(""),
    read: () => Promise.resolve([]),
  };
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: recorder });
  window.__sales0_clipboard_hooked = true;
  return true;
}"""

# Returns and clears the captured strings.
TAKE_COPIED_JS = r"""() => {
  const out = Array.isArray(window.__sales0_copied) ? window.__sales0_copied.slice() : [];
  window.__sales0_copied = [];
  return out;
}"""

# The post's control-menu trigger: locate the post by [componentkey="<KEY>"] (exact attribute
# value, no CSS escaping needed), else the n-th outermost post; then the first button /
# [role=button] inside its control menu (or the menu element itself if it is one). null = none.
FIND_TRIGGER_JS = r"""(a) => {
  if (a.tier === "listitem") {
    const stripRe = new RegExp(a.key_prefix_re);
    const outermost = (els) => els.filter((p) => !(p.parentElement && p.parentElement.closest(a.listitem_card)));
    const label = (card) => { const b = card.querySelector(a.listitem_menu); return b ? (b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim() : null; };
    let card = null;
    if (a.key) {
      for (const el of document.querySelectorAll("[" + a.key_attr + "]")) {
        const v = (el.getAttribute(a.key_attr) || "").trim();
        if (v !== a.key && v.replace(stripRe, "") !== a.key) continue;
        card = el.closest(a.listitem_card);
        if (card && card.querySelector(a.listitem_menu)) break;
        card = null;
      }
    }
    if (!card) {
      const cards = outermost(Array.from(document.querySelectorAll(a.listitem_card)))
        .filter((li) => a.listitem_markers.some((m) => li.querySelector(m)));
      const cand = cards[a.index] || null;
      // The index fallback is trusted only when the card's menu label matches what was extracted.
      if (cand && a.menu_label && label(cand) === a.menu_label) card = cand;
    }
    return card ? card.querySelector(a.listitem_menu) : null;
  }
  const outermost = (els) => els.filter((p) => !(p.parentElement && p.parentElement.closest(a.post_sel)));
  const labelRe = new RegExp(a.trigger_label_re, "i");
  const norm = (t) => (t || "").replace(/\s+/g, " ").trim().toLowerCase();
  const labelOf = (b) => (b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  // The post's menu trigger: only a button whose aria-label says it is the control menu.
  const triggerOf = (post) => {
    const menu = post.querySelector(a.menu_sel);
    if (!menu) return null;
    const cands = menu.matches(a.trigger_sel) ? [menu] : Array.from(menu.querySelectorAll(a.trigger_sel));
    return cands.find((b) => labelRe.test(labelOf(b))) || null;
  };
  let post = null;
  if (a.key) {
    for (const el of document.querySelectorAll("[" + a.key_attr + "]")) {
      if (el.getAttribute(a.key_attr) !== a.key) continue;
      post = el.matches(a.post_sel) ? el : (el.querySelector(a.post_sel) || el.closest(a.post_sel));
      if (post) break;
    }
  }
  if (post) return triggerOf(post);
  // Index fallback (key gone): never trusted on its own. A raw without a card_index (earlier pass)
  // has index -1 and gets nothing; otherwise the trigger's label AND the post's leading text must match.
  if (a.index < 0 || !a.menu_label) return null;
  const cand = outermost(Array.from(document.querySelectorAll(a.post_sel)))[a.index] || null;
  if (!cand) return null;
  const trig = triggerOf(cand);
  if (!trig || labelOf(trig) !== a.menu_label) return null;
  if (a.text_head && !norm(cand.innerText).includes(a.text_head)) return null;
  return trig;
}"""

# Polled by wait_for_function: the innermost VISIBLE element whose trimmed text equals the
# "Copy link to post" label, first inside the post's control menu, else inside a visible
# document-level [role="menu"] (menus rendered as a portal). Falsy until it appears.
FIND_COPY_ITEM_JS = r"""([trigger, a]) => {
  const visible = (e) => {
    if (!e || !e.isConnected) return false;
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };
  const label = (e) => (e.innerText || "").trim().toLowerCase();
  const pick = (scopes) => {
    const hits = [];
    for (const scope of scopes) {
      for (const e of scope.querySelectorAll("*")) if (label(e) === a.text && visible(e)) hits.push(e);
    }
    const innermost = hits.filter((e) => !hits.some((o) => o !== e && e.contains(o)));
    return innermost[0] || null;
  };
  const post = trigger && trigger.isConnected ? (trigger.closest(a.post_sel) || trigger.closest(a.listitem_card)) : null;
  const menus = post ? Array.from(post.querySelectorAll(a.menu_sel)) : [];
  // Phase 1.4 (listitem tier): the menu is usually a portal; last resort, any visible innermost
  // element of the document whose text is exactly the label.
  return pick(menus) || pick(Array.from(document.querySelectorAll(a.doc_menu_sel)).filter(visible))
    || pick([document.body]) || null;
}"""


# Phase 1.5 (live finding): LinkedIn's "Copy link to post" now copies a SHORT link
# (``https://lnkd.in/p/<code>``), which is not a same-site post URL, so every successful capture was
# discarded. A short link is resolved with ONE anonymous, cookie-less HEAD to lnkd.in (no LinkedIn
# session, not a scraper action on the account); its 301 ``Location`` is the real post URL.
SHORT_LINK_HOSTS = frozenset({"lnkd.in", "www.lnkd.in"})
SHORT_LINK_TOTAL_BUDGET_S = 5.0  # ONE budget shared by every HEAD hop of one short link
SHORT_LINK_MAX_HOPS = 2
# Used only when the page cannot report its own User-Agent.
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_HIGHLIGHT_URN = "highlightedUpdateUrn"
_POST_URN_RE = r"^urn:li:(activity|ugcPost|share):\d+$"
_REDIRECTS = (301, 302, 303, 307, 308)


def is_short_link(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in SHORT_LINK_HOSTS and parts.path.startswith("/p/")


def _fetch_location(url: str, *, proxy_url: str | None = None, user_agent: str | None = None) -> str | None:
    """The ``Location`` of the short link's redirect (no cookies, redirects not followed by httpx).

    Goes through the account's proxy (so the request leaves from the same IP as the account) with the
    browser's own User-Agent. At most ``SHORT_LINK_MAX_HOPS`` HEADs share ONE ``SHORT_LINK_TOTAL_BUDGET_S``
    deadline (a short link that bounces to another short link is followed while budget remains)."""
    deadline = time.monotonic() + SHORT_LINK_TOTAL_BUDGET_S
    headers = {"Accept": "*/*", "User-Agent": user_agent or FALLBACK_USER_AGENT}
    current = url
    for _ in range(SHORT_LINK_MAX_HOPS):
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            return None
        kwargs: dict[str, Any] = {"headers": headers, "timeout": remaining, "follow_redirects": False}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        try:
            with httpx.Client(**kwargs) as client:
                resp = client.head(current)
        except (httpx.HTTPError, ValueError):
            return None
        location = resp.headers.get("location") if resp.status_code in _REDIRECTS else None
        if not location:
            return None
        if not is_short_link(location):
            return location
        current = location
    return None


def resolve_short_link(
    copied: str,
    fetch: Callable[[str], str | None] | None = None,
    *,
    proxy_url: str | None = None,
    user_agent: str | None = None,
) -> str:
    """``lnkd.in/p/<code>`` -> the post URL it points at; any other string is returned unchanged.

    The result is ``https://www.linkedin.com/feed/update/urn:li:activity:N/`` when the redirect names
    the activity (``highlightedUpdateUrn``), else the ``/posts/...`` path without its query. When the
    short link cannot be resolved the original string is returned; ``capture_permalinks`` then counts it as a
    MISS (``is_short_link``) and never stores it.
    """
    try:
        parts = urlsplit(copied.strip())
    except ValueError:
        return copied
    if parts.scheme != "https" or (parts.hostname or "").lower() not in SHORT_LINK_HOSTS or not parts.path.startswith("/p/"):
        return copied
    if fetch is not None:
        location = fetch(copied.strip())
    else:
        location = _fetch_location(copied.strip(), proxy_url=proxy_url, user_agent=user_agent)
    if not location:
        return copied
    try:
        target = urlsplit(location)
    except ValueError:
        return copied
    host = (target.hostname or "").lower()
    if target.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return copied
    urn = (parse_qs(target.query).get(_HIGHLIGHT_URN) or [""])[0]
    urn = unquote(urn)
    if re.match(_POST_URN_RE, urn):
        return f"{target.scheme}://{target.netloc}/feed/update/{urn}/"
    if target.path.startswith("/posts/") or "/feed/update/" in target.path:
        return f"{target.scheme}://{target.netloc}{target.path}"
    return copied


@dataclass(frozen=True)
class PermalinkStats:
    attempts: int = 0  # "Copy link to post" attempts (posts whose menu sales0 tried to open)
    found: int = 0  # attempts that yielded a copied URL


def _has_urn(raw: dict[str, Any]) -> bool:
    urn = raw.get("urn")
    if isinstance(urn, str) and URN_RE.match(urn.strip()):
        return True
    return urn_from_href(raw.get("post_href")) is not None


def eligible_indices(raws: list[dict[str, Any]], cap: int) -> list[int]:
    """Indices (DOM order) of the first ``cap`` raws without a URN, with a component key and a
    control menu."""
    out: list[int] = []
    if cap <= 0:
        return out
    for index, raw in enumerate(raws):
        if len(out) >= cap:
            break
        key = raw.get("component_key")
        if _has_urn(raw) or not (isinstance(key, str) and key.strip()) or not raw.get("has_control_menu"):
            continue
        out.append(index)
    return out


def ref_of_raw(raw: dict[str, Any]) -> str | None:
    """The ref ScrapedPost.source_ref will carry for ``raw`` before any capture: URN, else ck:<KEY>."""
    urn = raw.get("urn")
    if isinstance(urn, str) and URN_RE.match(urn.strip()):
        return urn.strip()
    urn = urn_from_href(raw.get("post_href"))
    if urn:
        return urn
    key = raw.get("component_key")
    return f"{S.SOURCE_REF_KEY_PREFIX}{key.strip()}" if isinstance(key, str) and key.strip() else None


def eligible_refs(raws: list[dict[str, Any]], refs: Sequence[str], cap: int) -> list[str]:
    """The first ``cap`` of ``refs`` (in the order given, each once) whose card can be captured: a
    raw with that ref, no URN yet, a component key and a control menu (``sdui`` and ``listitem`` tiers)."""
    out: list[str] = []
    if cap <= 0:
        return out
    by_ref = {ref: raw for raw in raws if (ref := ref_of_raw(raw)) is not None}
    for ref in refs:
        if len(out) >= cap:
            break
        raw = by_ref.get(ref)
        if raw is None or ref in out or _has_urn(raw) or not raw.get("has_control_menu"):
            continue
        out.append(ref)
    return out


# Labels of a post's menu trigger ("Open control menu for post by X", "More actions"); anything else
# in the control-menu container (Follow, Hide ...) is not the trigger.
TRIGGER_LABEL_RE = r"control menu|more actions"
TEXT_HEAD_CHARS = 30


def _text_head(raw: dict[str, Any]) -> str:
    text = raw.get("text")
    return " ".join(text.split()).lower()[:TEXT_HEAD_CHARS] if isinstance(text, str) else ""


def _trigger_args(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """``index`` (the position in the capture loop) is deliberately NOT a fallback: only the card's own
    ``card_index`` (set for the page that is loaded) is, and a raw without one gets -1 (key lookup only)."""
    pos = raw.get("card_index")
    return {
        "tier": "listitem" if raw.get("strategy") == "listitem" else "sdui",
        "key": (raw.get("component_key") or "").strip(),
        "index": pos if isinstance(pos, int) and not isinstance(pos, bool) else -1,
        "trigger_label_re": TRIGGER_LABEL_RE,
        "text_head": _text_head(raw),
        "menu_label": (raw.get("menu_label") or "").strip(),
        "key_attr": S.SDUI_KEY_ATTR,
        "key_prefix_re": S.LISTITEM_KEY_PREFIX_RE,
        "post_sel": S.SDUI_POST,
        "menu_sel": S.SDUI_CONTROL_MENU,
        "trigger_sel": _TRIGGER,
        "listitem_card": S.LISTITEM_CARD,
        "listitem_markers": list(S.LISTITEM_POST_MARKERS),
        "listitem_menu": S.LISTITEM_MENU,
    }


def _item_args() -> dict[str, Any]:
    return {
        "text": S.COPY_LINK_TEXT.strip().lower(),
        "post_sel": S.SDUI_POST,
        "listitem_card": S.LISTITEM_CARD,
        "menu_sel": S.SDUI_CONTROL_MENU,
        "doc_menu_sel": _DOC_MENU,
    }


# Raw page actions, module-level so tests can observe them.
def _click(element: ElementHandle) -> None:
    element.click(timeout=CLICK_TIMEOUT_MS)


def _center(page: Page, element: ElementHandle, pacer: Pacer | None = None) -> None:
    """Scroll the trigger to the middle of the viewport, let the card settle, then hover it.
    After a deep results scroll the first cards sit far above the viewport (and under LinkedIn's
    sticky header); a click without settling opened no menu live (Phase 1.4 live check). The settle
    waits are jittered (never the same two numbers every time)."""
    pacer = pacer or _DEFAULT_PACER
    try:
        element.evaluate("(e) => e.scrollIntoView({block: 'center', inline: 'nearest'})")
        page.wait_for_timeout(pacer.jitter_ms(700))
        element.hover(timeout=CLICK_TIMEOUT_MS)
        page.wait_for_timeout(pacer.jitter_ms(250))
    except Exception:  # noqa: BLE001 - best-effort; the click that follows decides
        pass


def _press_escape(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
    except PlaywrightError:
        pass  # the next checkpoint decides whether the page is still usable


def _take(page: Page) -> list[str]:
    values = page.evaluate(TAKE_COPIED_JS) or []
    return [v for v in values if isinstance(v, str) and v.strip()]


def _poll_copied(page: Page) -> str | None:
    for poll in range(TAKE_POLLS):
        try:
            copied = _take(page)
        except PlaywrightError:
            return None
        if copied:
            return copied[-1].strip()  # the last captured string
        if poll < TAKE_POLLS - 1:
            page.wait_for_timeout(TAKE_POLL_MS)
    return None


def _find_trigger(page: Page, raw: dict[str, Any], index: int) -> ElementHandle | None:
    handle = page.evaluate_handle(FIND_TRIGGER_JS, _trigger_args(raw, index))
    element = handle.as_element()
    if element is None:
        handle.dispose()
    return element


def _attempt(
    page: Page,
    raw: dict[str, Any],
    index: int,
    pacer: Pacer,
    checkpoint: Callable[[], object],
    proxy_url: str | None = None,
    user_agent: str | None = None,
) -> tuple[Outcome, str | None]:
    # 1. Open the post's "…" menu (normal action delay first).
    pacer.delay()
    try:
        trigger = _find_trigger(page, raw, index)
        if trigger is None:
            checkpoint()
            return "no_card", None  # the card is no longer in the DOM: neither an attempt nor a miss
        _center(page, trigger, pacer)
        _click(trigger)
    except PlaywrightError:
        checkpoint()
        return "click_error", None
    checkpoint()

    # 2. Wait for "Copy link to post" and click it (and nothing else).
    pacer.short()
    outcome: Outcome = "no_menu_item"
    copied: str | None = None
    item: ElementHandle | None = None
    try:
        item = page.wait_for_function(FIND_COPY_ITEM_JS, arg=[trigger, _item_args()], timeout=MENU_ITEM_TIMEOUT_MS).as_element()
    except PlaywrightError:
        item = None  # includes the timeout: the item never appeared
    if item is not None:
        try:
            _take(page)  # drop anything the page wrote before this click
            _click(item)
            outcome = "no_copy"
        except PlaywrightError:
            outcome = "click_error"
        checkpoint()
        # 3. Read what the page tried to copy.
        if outcome == "no_copy":
            copied = _poll_copied(page)
            if copied:
                copied = resolve_short_link(copied, proxy_url=proxy_url, user_agent=user_agent)
                if is_short_link(copied):
                    copied, outcome = None, "unresolved"  # a short link is not a post URL: a miss
                else:
                    outcome = "hit"

    pacer.short()
    _press_escape(page)
    checkpoint()
    return outcome, copied


def capture_permalinks(
    page: Page,
    raws: list[dict[str, Any]],
    *,
    cap: int,
    pacer: Pacer,
    checkpoint: Callable[[], object],
    keyword: str = "",
    proxy_url: str | None = None,
    user_agent: str | None = None,
) -> PermalinkStats:
    """Try "Copy link to post" on the eligible raws; set ``raw["copied_url"]`` on each hit.

    ``checkpoint`` is the searcher's safety checkpoint: it raises ``AccountSafetyStop`` (browser
    already closed), which propagates unchanged.
    """
    targets = eligible_indices(raws, len(raws)) if cap > 0 else []
    if not targets:
        return PermalinkStats()
    search_path = urlsplit(page.url).path
    try:
        page.evaluate(CLIPBOARD_HOOK_JS)
    except PlaywrightError as exc:
        checkpoint()  # a stop wins; otherwise skip capture for this keyword
        log.debug("scraper_permalink_skipped", keyword=keyword, reason=f"hook: {type(exc).__name__}")
        return PermalinkStats()

    attempts = found = misses = 0
    for index in targets:
        if attempts >= cap:
            break
        if urlsplit(page.url).path != search_path:
            log.debug("scraper_permalink_stopped", keyword=keyword, reason="left_search_page", index=index)
            break
        outcome, copied = _attempt(page, raws[index], index, pacer, checkpoint, proxy_url, user_agent)
        log.debug("scraper_permalink", keyword=keyword, index=index, outcome=outcome)
        if outcome == "no_card":
            continue
        attempts += 1
        if copied:
            raws[index]["copied_url"] = copied
            found += 1
            misses = 0
            continue
        misses += 1
        if misses >= MAX_CONSECUTIVE_MISSES:
            log.debug("scraper_permalink_stopped", keyword=keyword, reason="consecutive_misses", index=index)
            break
    return PermalinkStats(attempts=attempts, found=found)

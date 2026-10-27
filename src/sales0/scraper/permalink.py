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
- At most ``cap`` attempts per keyword (0 = no hook, no clicks); 3 consecutive misses end
  capture for the keyword; leaving the search page ends it too.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.sync_api import ElementHandle, Page
from playwright.sync_api import Error as PlaywrightError

from sales0.logging import get_logger
from sales0.scraper import selectors as S
from sales0.scraper.pacing import Pacer
from sales0.scraper.parsing import URN_RE, urn_from_href

log = get_logger("sales0.scraper.permalink")

Outcome = Literal["hit", "no_menu_item", "no_copy", "click_error"]

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
  const outermost = (els) => els.filter((p) => !(p.parentElement && p.parentElement.closest(a.post_sel)));
  let post = null;
  if (a.key) {
    for (const el of document.querySelectorAll("[" + a.key_attr + "]")) {
      if (el.getAttribute(a.key_attr) !== a.key) continue;
      post = el.matches(a.post_sel) ? el : (el.querySelector(a.post_sel) || el.closest(a.post_sel));
      if (post) break;
    }
  }
  if (!post) post = outermost(Array.from(document.querySelectorAll(a.post_sel)))[a.index] || null;
  if (!post) return null;
  const menu = post.querySelector(a.menu_sel);
  if (!menu) return null;
  return menu.matches(a.trigger_sel) ? menu : menu.querySelector(a.trigger_sel);
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
  const post = trigger && trigger.isConnected ? trigger.closest(a.post_sel) : null;
  const menus = post ? Array.from(post.querySelectorAll(a.menu_sel)) : [];
  return pick(menus) || pick(Array.from(document.querySelectorAll(a.doc_menu_sel)).filter(visible)) || null;
}"""


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


def _trigger_args(raw: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "key": (raw.get("component_key") or "").strip(),
        "index": index,
        "key_attr": S.SDUI_KEY_ATTR,
        "post_sel": S.SDUI_POST,
        "menu_sel": S.SDUI_CONTROL_MENU,
        "trigger_sel": _TRIGGER,
    }


def _item_args() -> dict[str, Any]:
    return {
        "text": S.COPY_LINK_TEXT.strip().lower(),
        "post_sel": S.SDUI_POST,
        "menu_sel": S.SDUI_CONTROL_MENU,
        "doc_menu_sel": _DOC_MENU,
    }


# Raw page actions, module-level so tests can observe them.
def _click(element: ElementHandle) -> None:
    element.click(timeout=CLICK_TIMEOUT_MS)


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
    page: Page, raw: dict[str, Any], index: int, pacer: Pacer, checkpoint: Callable[[], object]
) -> tuple[Outcome, str | None]:
    # 1. Open the post's "…" menu (normal action delay first).
    pacer.delay()
    try:
        trigger = _find_trigger(page, raw, index)
        if trigger is None:
            checkpoint()
            return "click_error", None
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
) -> PermalinkStats:
    """Try "Copy link to post" on the eligible raws; set ``raw["copied_url"]`` on each hit.

    ``checkpoint`` is the searcher's safety checkpoint: it raises ``AccountSafetyStop`` (browser
    already closed), which propagates unchanged.
    """
    targets = eligible_indices(raws, cap)
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
        if urlsplit(page.url).path != search_path:
            log.debug("scraper_permalink_stopped", keyword=keyword, reason="left_search_page", index=index)
            break
        attempts += 1
        outcome, copied = _attempt(page, raws[index], index, pacer, checkpoint)
        log.debug("scraper_permalink", keyword=keyword, index=index, outcome=outcome)
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

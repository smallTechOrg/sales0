"""ALL LinkedIn DOM selectors, in one place (architecture.md → "## LinkedIn browser").

Phase 1.1: current LinkedIn ships obfuscated, random-looking class names, so nothing here
depends on a class except the LAST fallback of a field (``LEGACY_*``, Phase 1's markup).
Cards are found by their URN attributes and post links; every field has ordered fallbacks
and the first non-empty value wins. No code may require a logged-in DOM marker: logged-in
markers are soft hints only (logged, never part of a verdict).

Phase 1.2: current search results ship LinkedIn's SDUI markup (no URN anywhere, no post
links). Its stable anchors are ``data-view-name``, ``data-testid``, ``role="listitem"`` and
``componentkey`` (architecture.md → "## LinkedIn SDUI search results"). Card discovery tries
the SDUI posts first and falls back to the Phase 1.1 URN layout.

When LinkedIn changes its markup, fix it here (plus the fixture templates / saved samples
when the shape is new) and nowhere else.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- session
# The session cookie LinkedIn sets on login (the fixture sets the same name).
SESSION_COOKIE = "li_at"

# --------------------------------------------------------------------------- SDUI (Phase 1.2)
# One post (outermost only). If >= 1 is found the "sdui" strategy wins for the page.
SDUI_POST = '[data-view-name="feed-full-update"]'
# The post's wrapper; also the drift container count when no post is found.
SDUI_LISTITEM = '[role="listitem"][componentkey]'
# KEY from the wrapper's componentkey ("expanded<KEY>FeedType_FLAGSHIP_SEARCH"); JS- and Python-compatible.
SDUI_LISTITEM_KEY_RE = r"^expanded(.+?)FeedType_[A-Za-z_]+$"
# The inner div[componentkey="<KEY>"] (preferred source of KEY).
SDUI_KEY_ATTR = "componentkey"
# ck:<KEY> must fit enquiry_sources.source_ref String(200).
SDUI_KEY_MAX_LEN = 197
SOURCE_REF_KEY_PREFIX = "ck:"
# Author block (avatar link + name/meta).
SDUI_ACTOR = '[data-view-name="feed-actor"]'
# Post text, in order.
SDUI_TEXT: tuple[str, ...] = ('[data-view-name="feed-commentary"]', '[data-testid="expandable-text-box"]')
# "…more" (the first entry of SEE_MORE_ARIA_SELECTORS' chain).
SDUI_EXPAND = '[data-testid="expandable-text-button"]'
# Text candidates inside these are ignored (article titles, document captions).
SDUI_TEXT_EXCLUDE: tuple[str, ...] = (
    '[data-view-name="feed-update-image"]',
    '[data-view-name="feed-article"]',
    '[data-view-name="feed-document-container"]',
)
# The post's "…" menu (trigger + items) and the one item sales0 ever clicks in it.
SDUI_CONTROL_MENU = '[data-view-name="feed-control-menu"]'
COPY_LINK_TEXT = "copy link to post"
# Infinite-scroll column; the mouse is moved over it before each wheel.
SDUI_SCROLL_CONTAINER = '[data-testid="lazy-column"]'
# Informational only (logged as sdui_screen), never a verdict.
SDUI_SCREEN = '[data-sdui-screen*="SearchResultsContent"]'

# --------------------------------------------------------------------------- listitem variant (Phase 1.3)
# LinkedIn serves the same SDUI results page in several markup variants; one has NO data-view-name
# at all. What every variant keeps is the visible structure: a role=listitem holding a hidden
# "Feed post" heading, a profile link, an expandable text box, a relative time and a
# "Open control menu for post by <Name>" button. This tier only needs those, and is tried after
# the data-view-name (sdui) and URN strategies - and also wins when they yield no post text.
LISTITEM_CARD = '[role="listitem"]'
LISTITEM_POST_MARKERS: tuple[str, ...] = (
    '[data-testid="expandable-text-box"]',
    '[data-view-name="feed-commentary"]',
    'button[aria-label^="Open control menu for post by"]',
)
LISTITEM_TEXT: tuple[str, ...] = ('[data-testid="expandable-text-box"]', '[data-view-name="feed-commentary"]')
LISTITEM_MENU = 'button[aria-label^="Open control menu for post by"]'
LISTITEM_MENU_LABEL_RE = r"^Open control menu for post by\s+(.+?)\s*$"
LISTITEM_KEY_PREFIX_RE = r"^(?:update-card-focus|expanded)"

# --------------------------------------------------------------------------- cards (URN layout)
URN_PREFIXES: tuple[str, ...] = ("urn:li:activity:", "urn:li:ugcPost:", "urn:li:share:")
URN_ATTRIBUTES: tuple[str, ...] = ("data-urn", "data-id")

# 1. Cards carrying a post URN attribute (outermost only: a nested match is a reshared original).
CARD_ATTR_SELECTORS: tuple[str, ...] = tuple(
    f'[{attr}^="{prefix}"]' for attr in URN_ATTRIBUTES for prefix in URN_PREFIXES
)
# 2. Post permalinks; a card is the anchor's closest CARD_CONTAINERS ancestor.
POST_LINK_SELECTORS: tuple[str, ...] = (
    'a[href*="/feed/update/urn:li:activity:"]',
    'a[href*="/feed/update/urn:li:ugcPost:"]',
    'a[href*="/feed/update/urn:li:share:"]',
    'a[href*="/posts/"]',
)
CARD_CONTAINERS = 'li, article, [role="article"], [data-view-name*="update" i], [data-view-name*="search-entity" i]'

# URN from an href (JS-compatible regex sources; also compiled in ``urls.py``).
FEED_UPDATE_HREF_RE = r"/feed/update/(urn:li:(?:activity|ugcPost|share):\d+)"
# Any post URN in an attribute value (SDUI strategy: best effort).
ANY_URN_RE = r"urn:li:(?:activity|ugcPost|share):\d+"
POSTS_SLUG_HREF_RE = r"/posts/[^/?#]*-(activity|ugcPost|share)-(\d+)"

# --------------------------------------------------------------------------- fields
AUTHOR_ANCHOR = 'a[href*="/in/"], a[href*="/company/"]'
# author_name, in order (1–2 inside the author anchor; 3 = its aria-label; 4 = its first text line).
AUTHOR_NAME_IN_ANCHOR: tuple[str, ...] = ('[aria-hidden="true"]', "span[dir]")
AUTHOR_ARIA_LABEL_STRIP: tuple[str, ...] = ("View ", "’s profile", "'s profile", "’ profile", "' profile", ", graphic")
LEGACY_AUTHOR_NAME = '.update-components-actor__title span[aria-hidden="true"]'
LEGACY_AUTHOR_PROFILE_LINK = "a.update-components-actor__meta-link"
LEGACY_AUTHOR_HEADLINE = '.update-components-actor__description span[aria-hidden="true"]'
LEGACY_RELATIVE_TIME = '.update-components-actor__sub-description span[aria-hidden="true"]'
# Lines of the author block that are never a headline.
HEADLINE_SKIP_RE = (
    r"^(?:[•·]\s*)?(?:1st|2nd|3rd\+?|\d+(?:st|nd|rd|th)\+?|\+?\s*follow|following|premium|verified)$"
    r"|degree connection$"
)

# Post text: the LONGEST candidate wins; a tie goes to the earlier selector.
TEXT_SELECTORS: tuple[str, ...] = (
    '[data-test-id*="commentary" i]',
    '[dir="ltr"]',
    "span[dir]",
    ".update-components-text",  # legacy
)

# Relative time (JS- and Python-compatible sources; matched case-insensitively).
RELATIVE_TIME_SHORT_RE = r"^\s*(\d+)\s*(mins|min|mo|m|hrs|hr|h|s|d|wk|w|yrs|yr|y)\b"
RELATIVE_TIME_LONG_RE = r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago"

# "See more", in order. Never an a[href], never an aria-label matching SEE_MORE_EXCLUDE_RE.
SEE_MORE_ARIA_SELECTORS: tuple[str, ...] = (
    SDUI_EXPAND,  # Phase 1.2: the SDUI "…more" button comes first
    'button[aria-label*="see more" i]',
    'button[aria-label*="show more" i]',
    '[role="button"][aria-label*="see more" i]',
)
SEE_MORE_TEXT_CANDIDATES = 'button, [role="button"]'
# Audit #15: needs an ellipsis or a see/show prefix - a bare "More" (profile/nav "More" menu) never matches.
SEE_MORE_TEXT_RE = r"^(?:(?:…|\.\.\.)\s*(?:(?:see|show)\s*)?|(?:see|show)\s*)more$"
LEGACY_SEE_MORE = "button.feed-shared-inline-show-more-text__see-more-less-toggle"
SEE_MORE_EXCLUDE_RE = r"actions|options|menu|comment|reaction"

# --------------------------------------------------------------------------- page markers
LEGACY_NO_RESULTS = ".search-reusable-search-no-results"
NO_RESULTS_TEXT_TAGS = "h1, h2, h3, p"
NO_RESULTS_PREFIX = "no results found"
NO_RESULTS_EXACT = "no results"

# Logged-out markers (additional logged-out signals only).
LOGIN_FORM_MARKERS: tuple[str, ...] = ("input#username", 'input[name="session_key"]', 'form[action*="login"]')
AUTHWALL_MARKERS: tuple[str, ...] = (".authwall", '[data-test-id*="authwall"]', "#authwall")
CAPTCHA_MARKERS: tuple[str, ...] = (
    'iframe[src*="captcha" i]',
    'iframe[src*="recaptcha"]',
    'iframe[src*="arkoselabs"]',
    'iframe[src*="funcaptcha"]',
    'iframe[title*="captcha" i]',
    "#captcha-internal",
    'form[action*="captcha"]',
)
# Soft logged-in hints: name → selector. Reported by name; NEVER used in a verdict.
SOFT_LOGGED_IN_HINTS: dict[str, str] = {
    "self_profile_link": 'a[href*="/in/"]',
    "global_nav_attr": "[data-test-global-nav]",
    "nav_aria": "nav[aria-label]",
    "main_landmark": 'main, [role="main"]',
    "legacy_global_nav": "#global-nav, header.global-nav",
}

# verify_in_context waits (bounded) for any of these to be attached; missing content never fails it.
SESSION_CONTENT_READY = 'main, [role="main"], form, iframe'


def join(selectors: tuple[str, ...] | list[str]) -> str:
    """One CSS selector list from ordered alternatives."""
    return ", ".join(selectors)

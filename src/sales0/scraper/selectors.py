"""ALL DOM selectors in one place (architecture.md → DOM contract).

The fixture templates in ``sales0/linkedin_fixture/templates`` use exactly these. When
LinkedIn changes its markup, fix it here (plus the fixture templates) and nowhere else.
"""

from __future__ import annotations

# One search-result post card; the URN comes from the data-urn attribute. LinkedIn renders
# activity URNs on most cards, but some cards carry the ugcPost/share URN instead.
URN_PREFIXES = ("urn:li:activity:", "urn:li:ugcPost:", "urn:li:share:")
RESULT_CARD = ", ".join(f'div[data-urn^="{prefix}"]' for prefix in URN_PREFIXES)
URN_ATTRIBUTE = "data-urn"

AUTHOR_NAME = '.update-components-actor__title span[aria-hidden="true"]'
AUTHOR_PROFILE_LINK = "a.update-components-actor__meta-link"
AUTHOR_HEADLINE = '.update-components-actor__description span[aria-hidden="true"]'
RELATIVE_TIME = '.update-components-actor__sub-description span[aria-hidden="true"]'
POST_TEXT = ".update-components-text"
SEE_MORE_BUTTON = "button.feed-shared-inline-show-more-text__see-more-less-toggle"

# Present on every logged-in LinkedIn page. Real LinkedIn renders
# ``<header id="global-nav" class="global-nav ..."><nav class="global-nav__nav">``; the
# fixture renders ``<nav class="global-nav">``. Any one of them counts.
LOGGED_IN_MARKER = "#global-nav, header.global-nav, nav.global-nav__nav, nav.global-nav"
NO_RESULTS_MARKER = ".search-reusable-search-no-results"
CAPTCHA_MARKER = 'iframe[src*="captcha"]'

# The session cookie LinkedIn sets on login (fixture sets the same name).
SESSION_COOKIE = "li_at"

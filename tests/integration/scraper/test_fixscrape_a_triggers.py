"""Slice A (#1, #9): permalink trigger lookup on real headless Chromium against static SDUI markup.

The wrong-post scenario: the card's component key is no longer in the DOM and its remembered
``card_index`` points at ANOTHER post. The old code opened that post's menu and attached its URL.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from sales0.scraper import permalink as P

pytestmark = pytest.mark.browser


def _post(key: str, name: str, text: str, buttons: str | None = None) -> str:
    buttons = buttons if buttons is not None else f'<button type="button" aria-label="Open control menu for post by {name}">…</button>'
    return (
        f'<div data-view-name="feed-full-update" componentkey="{key}"><p>{name}</p><p>{text}</p>'
        f'<div data-view-name="feed-control-menu">{buttons}</div></div>'
    )


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    p = browser.new_page()
    yield p
    p.close()


def _raw(key: str, **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"strategy": "sdui", "component_key": key, "urn": None, "post_href": None, "has_control_menu": True}
    raw.update(over)
    return raw


def _label_of(page: Page, raw: dict[str, Any], index: int = 0) -> str | None:
    el = P._find_trigger(page, raw, index)
    return None if el is None else el.get_attribute("aria-label")


TWO = _post("KA", "Ann", "Ann wants a Copilot trainer") + _post("KB", "Bob", "Bob needs AI workshops")


def test_a_missing_key_with_a_stale_index_never_opens_the_other_post(page: Page) -> None:
    page.set_content(TWO)
    # post B's key vanished; its remembered index 0 is post A
    stale = _raw("GONE", card_index=0, menu_label="Open control menu for post by Bob", text="Bob needs AI workshops")
    assert _label_of(page, stale) is None


def test_a_raw_from_an_earlier_pass_has_no_index_and_is_found_only_by_its_key(page: Page) -> None:
    page.set_content(TWO)
    earlier = _raw("KB", menu_label="Open control menu for post by Bob")  # no card_index
    assert _label_of(page, earlier, index=0) == "Open control menu for post by Bob"
    missing = _raw("GONE", menu_label="Open control menu for post by Bob")
    assert _label_of(page, missing, index=0) is None  # the loop position 0 (= post A) must not stand in


def test_the_index_fallback_needs_label_and_text_to_match(page: Page) -> None:
    page.set_content(TWO)
    ok = _raw("GONE", card_index=1, menu_label="Open control menu for post by Bob", text="Bob needs AI workshops")
    assert _label_of(page, ok) == "Open control menu for post by Bob"
    wrong_label = _raw("GONE", card_index=1, menu_label="Open control menu for post by Ann", text="Bob needs AI workshops")
    assert _label_of(page, wrong_label) is None
    same_author_other_text = _raw("GONE", card_index=1, menu_label="Open control menu for post by Bob", text="something else entirely")
    assert _label_of(page, same_author_other_text) is None


def test_the_sdui_trigger_must_be_the_control_menu_button_not_the_first_button(page: Page) -> None:
    buttons = (
        '<button type="button" aria-label="Follow Ann">Follow</button>'
        '<button type="button" aria-label="Open control menu for post by Ann">…</button>'
    )
    page.set_content(_post("KA", "Ann", "hello", buttons))
    assert _label_of(page, _raw("KA")) == "Open control menu for post by Ann"


def test_more_actions_label_is_accepted_and_a_labelless_or_unrelated_button_is_not(page: Page) -> None:
    page.set_content(_post("KA", "Ann", "x", '<button aria-label="More actions">…</button>'))
    assert _label_of(page, _raw("KA")) == "More actions"
    page.set_content(_post("KA", "Ann", "x", '<button aria-label="Follow Ann">F</button><button>…</button>'))
    assert _label_of(page, _raw("KA")) is None

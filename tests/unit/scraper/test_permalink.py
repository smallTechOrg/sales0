"""Permalink capture logic with a fake page (pure; no browser): the hook/take JS strings,
eligibility, the per-keyword cap, the 3-consecutive-miss stop, the search-path guard, the
pacing + checkpoint order of every step, and that only "Copy link to post" is ever clicked.
The same logic runs against real headless Chromium in
``tests/integration/scraper/test_permalink_capture.py``."""

from __future__ import annotations

import random
import re
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from sales0.scraper import permalink as P
from sales0.scraper.errors import AccountSafetyStop
from sales0.scraper.pacing import Pacer

SEARCH = "https://www.linkedin.com/search/results/content/"
POST_URL = "https://www.linkedin.com/posts/someone_x-activity-7300000000000000099-AbCd?utm_source=share&rcm=X"


class Log:
    def __init__(self) -> None:
        self.events: list[str] = []


class FakeElement:
    def __init__(self, log: Log, name: str, on_click: Any = None, fail: bool = False) -> None:
        self.log, self.name, self._on_click, self._fail = log, name, on_click, fail

    def as_element(self) -> FakeElement:
        return self

    def click(self, timeout: float | None = None) -> None:
        self.log.events.append(f"click:{self.name}")
        if self._fail:
            raise PlaywrightError("element detached")
        if self._on_click:
            self._on_click()

    def dispose(self) -> None:
        pass


class NullHandle:
    def as_element(self) -> None:
        return None

    def dispose(self) -> None:
        pass


class FakeKeyboard:
    def __init__(self, log: Log) -> None:
        self.log = log

    def press(self, key: str) -> None:
        self.log.events.append(f"key:{key}")


class FakePage:
    """Each post: ("hit"|"noop"|"no_item"|"no_trigger"|"trigger_error"). A "hit" copy click
    writes POST_URL through the (hooked) clipboard."""

    def __init__(self, behaviours: list[str], log: Log) -> None:
        self.behaviours, self.log = behaviours, log
        self.url = SEARCH + "?keywords=x"
        self.keyboard = FakeKeyboard(log)
        self.copied: list[str] = []
        self.hook_installs = 0
        self.locate_args: list[dict[str, Any]] = []
        self._current: int | None = None

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == P.CLIPBOARD_HOOK_JS:
            self.hook_installs += 1
            self.log.events.append("hook")
            return True
        if js == P.TAKE_COPIED_JS:
            out, self.copied = self.copied, []
            return out
        raise AssertionError(f"unexpected evaluate: {js[:60]}")

    def evaluate_handle(self, js: str, arg: Any = None) -> Any:
        assert js == P.FIND_TRIGGER_JS
        self.locate_args.append(arg)
        index = arg["index"]
        self._current = index
        behaviour = self.behaviours[index]
        if behaviour == "no_trigger":
            return NullHandle()
        return FakeElement(self.log, "menu", fail=behaviour == "trigger_error")

    def wait_for_function(self, js: str, arg: Any = None, timeout: float | None = None) -> Any:
        assert js == P.FIND_COPY_ITEM_JS and timeout is not None and timeout <= 2_000
        assert isinstance(arg[0], FakeElement)  # scoped to the clicked trigger's post
        assert arg[1]["text"] == "copy link to post"
        behaviour = self.behaviours[self._current]  # type: ignore[index]
        if behaviour == "no_item":
            raise PlaywrightTimeoutError("Timeout 2000ms exceeded")
        on_click = (lambda: self.copied.append(POST_URL)) if behaviour == "hit" else None
        return FakeElement(self.log, "copy link to post", on_click=on_click)

    def wait_for_timeout(self, ms: float) -> None:
        pass


def _pacer(log: Log) -> Pacer:
    pacer = Pacer(0.4, 0.8, random.Random(1), lambda s: log.events.append("delay" if s >= 0.4 else "short"))
    return pacer


def _raw(i: int, **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "strategy": "sdui",
        "urn": None,
        "post_href": None,
        "component_key": f"KEY{i}",
        "has_control_menu": True,
        "text": f"post {i}",
    }
    raw.update(over)
    return raw


def _run(behaviours: list[str], raws: list[dict[str, Any]] | None = None, cap: int = 15, checkpoint: Any = None):
    log = Log()
    page = FakePage(behaviours, log)
    raws = raws if raws is not None else [_raw(i) for i in range(len(behaviours))]
    cp = checkpoint or (lambda: log.events.append("checkpoint"))
    stats = P.capture_permalinks(page, raws, cap=cap, pacer=_pacer(log), checkpoint=cp, keyword="k")  # type: ignore[arg-type]
    return stats, raws, page, log


# ----------------------------------------------------------------------- JS strings
def test_clipboard_hook_replaces_navigator_clipboard_and_never_calls_the_original() -> None:
    js = P.CLIPBOARD_HOOK_JS
    assert "Object.defineProperty(navigator" in js and "configurable: true" in js
    assert "__sales0_copied" in js and "writeText" in js and "write" in js
    assert "String(" in js and "Promise.resolve" in js
    # The original is never captured or invoked: no reference to the current clipboard object.
    assert not re.search(r"navigator\.clipboard\s*[.\[(]", js)
    assert "navigator.clipboard;" not in js and ".call(" not in js and ".apply(" not in js


def test_take_copied_returns_and_clears_the_captured_list() -> None:
    js = P.TAKE_COPIED_JS
    assert "__sales0_copied" in js and "= []" in js


def test_copy_item_search_uses_exact_text_and_innermost_match() -> None:
    js = P.FIND_COPY_ITEM_JS
    assert "trim()" in js and "toLowerCase()" in js and "===" in js
    assert '[role=\\"menu\\"]' in js or "[role=\"menu\"]" in js or "menu_sel" in js


# ----------------------------------------------------------------------- eligibility
def test_eligible_skips_urn_posts_keyless_posts_and_posts_without_a_menu() -> None:
    raws = [
        _raw(0, urn="urn:li:activity:7300000000000000001"),
        _raw(1, post_href="https://www.linkedin.com/feed/update/urn:li:ugcPost:7300000000000000002/"),
        _raw(2, component_key=None),
        _raw(3, has_control_menu=False),
        _raw(4),
        _raw(5, strategy="urn", component_key=None, has_control_menu=False),
        _raw(6, component_key=""),
        _raw(7),
    ]
    assert P.eligible_indices(raws, 15) == [4, 7]
    assert P.eligible_indices(raws, 1) == [4]
    assert P.eligible_indices(raws, 0) == []
    assert P.eligible_indices([], 5) == []


# ----------------------------------------------------------------------- capture
def test_a_hit_sets_copied_url_only_and_each_step_is_paced_and_checkpointed() -> None:
    stats, raws, page, log = _run(["hit"])
    assert stats == P.PermalinkStats(attempts=1, found=1)
    assert raws[0]["copied_url"] == POST_URL
    assert set(raws[0]) == set(_raw(0)) | {"copied_url"}  # never touches other keys
    assert log.events == [
        "hook",
        "delay", "click:menu", "checkpoint",
        "short", "click:copy link to post", "checkpoint",
        "short", "key:Escape", "checkpoint",
    ]
    assert page.hook_installs == 1
    assert page.locate_args[0]["key"] == "KEY0" and page.locate_args[0]["index"] == 0


def test_cap_zero_installs_no_hook_and_clicks_nothing() -> None:
    stats, raws, page, log = _run(["hit", "hit"], cap=0)
    assert stats == P.PermalinkStats(0, 0) and log.events == [] and page.hook_installs == 0
    assert all("copied_url" not in r for r in raws)


def test_no_eligible_posts_installs_no_hook() -> None:
    raws = [_raw(0, urn="urn:li:activity:7300000000000000001")]
    stats, _raws, page, log = _run(["hit"], raws=raws)
    assert stats == P.PermalinkStats(0, 0) and page.hook_installs == 0 and log.events == []


def test_cap_one_attempts_exactly_one_post() -> None:
    stats, raws, _page, log = _run(["noop", "hit", "hit"], cap=1)
    assert stats == P.PermalinkStats(1, 0)
    assert log.events.count("click:menu") == 1
    assert all("copied_url" not in r for r in raws)


def test_three_consecutive_misses_end_capture_for_the_keyword() -> None:
    stats, raws, _page, log = _run(["noop", "no_item", "trigger_error", "hit"])
    assert stats == P.PermalinkStats(3, 0)
    assert "copied_url" not in raws[3]
    assert log.events.count("click:menu") == 3


def test_a_hit_resets_the_miss_streak() -> None:
    stats, raws, _page, _log = _run(["noop", "noop", "hit", "noop", "noop", "hit"])
    assert stats == P.PermalinkStats(6, 2)
    assert [("copied_url" in r) for r in raws] == [False, False, True, False, False, True]


def test_only_the_copy_link_item_is_ever_clicked() -> None:
    _stats, _raws, _page, log = _run(["hit", "noop", "no_item", "hit"])
    clicks = [e for e in log.events if e.startswith("click:")]
    assert set(clicks) <= {"click:menu", "click:copy link to post"}


def test_a_missing_menu_item_still_closes_the_menu_with_escape() -> None:
    _stats, _raws, _page, log = _run(["no_item"])
    assert "click:copy link to post" not in log.events
    assert log.events[-3:] == ["short", "key:Escape", "checkpoint"]


def test_a_card_gone_from_the_dom_is_neither_an_attempt_nor_a_miss() -> None:
    stats, _raws, _page, log = _run(["no_trigger"])
    assert stats == P.PermalinkStats(0, 0)
    assert not [e for e in log.events if e.startswith("click:")]


def test_a_safety_stop_at_a_checkpoint_propagates_and_ends_capture() -> None:
    calls: list[int] = []

    def checkpoint() -> None:
        calls.append(1)
        if len(calls) == 2:  # after the copy click
            raise AccountSafetyStop("checkpoint", "https://www.linkedin.com/checkpoint/challenge/x")

    with pytest.raises(AccountSafetyStop) as stop:
        _run(["hit", "hit"], checkpoint=checkpoint)
    assert stop.value.kind == "checkpoint" and len(calls) == 2


def test_capture_stops_when_the_page_left_the_search_path() -> None:
    log = Log()
    page = FakePage(["hit", "hit", "hit"], log)

    def checkpoint() -> None:
        log.events.append("checkpoint")
        if log.events.count("checkpoint") == 3:  # end of the first attempt: page navigated away
            page.url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"

    raws = [_raw(i) for i in range(3)]
    stats = P.capture_permalinks(page, raws, cap=15, pacer=_pacer(log), checkpoint=checkpoint)  # type: ignore[arg-type]
    assert stats == P.PermalinkStats(1, 1)
    assert log.events.count("click:menu") == 1


def test_the_last_captured_string_wins() -> None:
    log = Log()
    page = FakePage(["hit"], log)
    page.copied = ["https://www.linkedin.com/posts/stale"]  # an earlier write on the page
    raws = [_raw(0)]
    P.capture_permalinks(page, raws, cap=15, pacer=_pacer(log), checkpoint=lambda: None)  # type: ignore[arg-type]
    assert raws[0]["copied_url"] == POST_URL


# ----------------------------------------------------------------------- Phase 1.4: refs + tiers
def test_eligible_refs_picks_capturable_refs_in_the_order_given_up_to_the_cap() -> None:
    raws = [
        _raw(0),
        _raw(1, urn="urn:li:activity:7300000000000000001"),
        _raw(2, has_control_menu=False),
        _raw(3, strategy="listitem", menu_label="Open control menu for post by X", card_index=3),
        _raw(4, component_key=None),
        _raw(5),
    ]
    refs = ["ck:KEY5", "ck:KEY0", "urn:li:activity:7300000000000000001", "ck:KEY2", "ck:KEY3", "ck:nope", "ck:KEY0"]
    assert P.eligible_refs(raws, refs, 15) == ["ck:KEY5", "ck:KEY0", "ck:KEY3"]
    assert P.eligible_refs(raws, refs, 2) == ["ck:KEY5", "ck:KEY0"]
    assert P.eligible_refs(raws, refs, 0) == [] and P.eligible_refs([], refs, 5) == []


def test_the_trigger_is_located_per_tier_by_key_then_card_index_verified_by_label() -> None:
    sdui = P._trigger_args(_raw(2), 7)
    assert sdui["tier"] == "sdui" and sdui["key"] == "KEY2" and sdui["index"] == 7
    li = P._trigger_args(_raw(4, strategy="listitem", card_index=9, menu_label="Open control menu for post by Jo"), 0)
    assert li["tier"] == "listitem" and li["index"] == 9 and li["menu_label"] == "Open control menu for post by Jo"
    assert li["listitem_menu"] == 'button[aria-label^="Open control menu for post by"]'
    js = P.FIND_TRIGGER_JS
    assert "a.menu_label" in js and "label(cand) === a.menu_label" in js  # a mismatch = no card


def test_the_copy_item_search_falls_back_to_the_portal_then_the_document() -> None:
    js = P.FIND_COPY_ITEM_JS
    assert "a.doc_menu_sel" in js and "pick([document.body])" in js and "a.listitem_card" in js


def test_cards_gone_from_the_dom_cost_no_attempt_and_do_not_count_toward_the_miss_streak() -> None:
    # 3 vanished cards, then a noop, then a hit: only 2 attempts, 1 found; no 3-miss stop was triggered.
    stats, raws, _page, log = _run(["no_trigger", "no_trigger", "no_trigger", "noop", "hit"])
    assert stats == P.PermalinkStats(2, 1)
    assert "copied_url" in raws[4] and log.events.count("click:menu") == 2


def test_the_attempt_cap_counts_attempts_not_skipped_cards() -> None:
    stats, raws, _page, _log = _run(["no_trigger", "hit", "hit", "hit"], cap=2)
    assert stats == P.PermalinkStats(2, 2)
    assert "copied_url" not in raws[3]

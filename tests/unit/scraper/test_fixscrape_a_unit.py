"""Scraper-audit slice A regressions (pure / fake-page; no browser): findings #1 (args), #3, #7, #8,
#9, #10, #14, #15, #18 of AUDIT_SCRAPER.md."""

from __future__ import annotations

import random
import re
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock

import pytest

from sales0.config import Settings
from sales0.scraper import permalink as P
from sales0.scraper import search as search_module
from sales0.scraper import selectors as S
from sales0.scraper.detection import is_rate_limit_response
from sales0.scraper.errors import AccountSafetyStop, ScrapeError
from sales0.scraper.pacing import Pacer
from sales0.scraper.search import LinkedInSearcher
from sales0.scraper.session_store import AccountHandle, AccountSpec

BASE = "https://www.linkedin.com"


def _settings(tmp_path: Any, **over: Any) -> Settings:
    return Settings(data_dir=str(tmp_path), **over)


def _pacer() -> Pacer:
    return Pacer(0.4, 0.8, random.Random(1), lambda s: None)


def _open_searcher(tmp_path: Any, **kw: Any) -> LinkedInSearcher:
    searcher = LinkedInSearcher(_settings(tmp_path), _pacer(), **kw)
    searcher._stack = ExitStack()
    searcher._context = MagicMock()
    searcher._page = MagicMock()
    return searcher


# ------------------------------------------------------------------ #1 / #9 trigger args
def _raw(i: int, **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "strategy": "sdui", "urn": None, "post_href": None, "component_key": f"KEY{i}",
        "has_control_menu": True, "text": f"post {i} about training", "menu_label": f"Open control menu for post by P{i}",
    }
    raw.update(over)
    return raw


def test_a_raw_without_card_index_gets_no_positional_fallback() -> None:
    # raws of earlier passes lose card_index; the loop position must never stand in for it
    assert P._trigger_args(_raw(2), 7)["index"] == -1
    assert P._trigger_args(_raw(2, card_index=3), 7)["index"] == 3


def test_sdui_trigger_args_carry_the_label_pattern_and_a_text_fingerprint() -> None:
    args = P._trigger_args(_raw(2), 0)
    assert args["menu_label"] == "Open control menu for post by P2"
    assert re.search(args["trigger_label_re"], "Open control menu for post by Jo", re.I)
    assert re.search(args["trigger_label_re"], "More actions", re.I)
    assert not re.search(args["trigger_label_re"], "Follow Jo", re.I)
    assert args["text_head"].startswith("post 2")


def test_the_sdui_tier_never_takes_the_nth_post_without_a_label_match() -> None:
    js = P.FIND_TRIGGER_JS
    sdui_part = js.split("const outermost = (els) => els.filter((p) => !(p.parentElement && p.parentElement.closest(a.post_sel)));")[1]
    assert "a.menu_label" in sdui_part and "a.trigger_label_re" in sdui_part


# ------------------------------------------------------------------ #15 bare "More"
@pytest.mark.parametrize("label", ["More", "more", "  More ", "MORE"])
def test_a_bare_more_button_is_not_a_see_more_toggle(label: str) -> None:
    assert not re.match(S.SEE_MORE_TEXT_RE, label.strip(), re.I)


@pytest.mark.parametrize("label", ["…more", "... more", "see more", "See more", "show more", "…see more", "…more"])
def test_real_see_more_labels_still_match(label: str) -> None:
    assert re.match(S.SEE_MORE_TEXT_RE, label.strip(), re.I)


# ------------------------------------------------------------------ #10 short links
class _ShortPage:
    """A page whose only hit copies ``copied`` through the clipboard hook."""

    def __init__(self, copied: str) -> None:
        self.url = BASE + "/search/results/content/?keywords=x"
        self._copied = copied
        self.buffer: list[str] = []
        self.keyboard = MagicMock()

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == P.TAKE_COPIED_JS:
            out, self.buffer = self.buffer, []
            return out
        if "userAgent" in js:
            return "Mozilla/5.0 (Test) Chrome/1"
        return True

    def evaluate_handle(self, js: str, arg: Any = None) -> Any:
        el = MagicMock()
        el.as_element.return_value = el
        return el

    def wait_for_function(self, js: str, arg: Any = None, timeout: Any = None) -> Any:
        el = MagicMock()
        el.as_element.return_value = el
        el.click.side_effect = lambda **k: self.buffer.append(self._copied)
        return el

    def wait_for_timeout(self, ms: float) -> None:
        pass


def test_an_unresolved_short_link_is_a_miss_not_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "_fetch_location", lambda *a, **k: None)
    raws = [_raw(i) for i in range(5)]
    page = _ShortPage("https://lnkd.in/p/abc123")
    stats = P.capture_permalinks(page, raws, cap=15, pacer=_pacer(), checkpoint=lambda: None, keyword="k")  # type: ignore[arg-type]
    assert stats.found == 0 and all("copied_url" not in r for r in raws)
    assert stats.attempts == 3  # three consecutive misses end capture


def test_a_resolved_short_link_is_still_a_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    loc = "https://www.linkedin.com/posts/x-share-1-aa/?highlightedUpdateUrn=urn%3Ali%3Aactivity%3A7521020692243492864"
    monkeypatch.setattr(P, "_fetch_location", lambda *a, **k: loc)
    raws = [_raw(0)]
    stats = P.capture_permalinks(_ShortPage("https://lnkd.in/p/abc123"), raws, cap=3, pacer=_pacer(), checkpoint=lambda: None)  # type: ignore[arg-type]
    assert stats.found == 1 and raws[0]["copied_url"].endswith("urn:li:activity:7521020692243492864/")


def test_short_link_head_uses_the_account_proxy_a_browser_ua_and_a_short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {"timeouts": []}

    class FakeResp:
        status_code = 301
        headers = {"location": "https://www.linkedin.com/posts/a-b-activity-1-x/"}

    class FakeClient:
        def __init__(self, **kw: Any) -> None:
            seen["init"] = kw

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def head(self, url: str, **kw: Any) -> FakeResp:
            seen["head"] = kw
            return FakeResp()

    monkeypatch.setattr(P.httpx, "Client", FakeClient)
    out = P._fetch_location("https://lnkd.in/p/x", proxy_url="http://proxy.example:3128", user_agent="Mozilla/5.0 Chrome/126")
    assert out and seen["init"]["proxy"] == "http://proxy.example:3128"
    headers = {k.lower(): v for k, v in seen["init"]["headers"].items()}
    assert headers["user-agent"] == "Mozilla/5.0 Chrome/126" and "httpx" not in headers["user-agent"].lower()
    timeout = seen["init"]["timeout"]
    total = timeout if isinstance(timeout, (int, float)) else max(timeout.connect or 0, timeout.read or 0)
    assert 0 < total <= P.SHORT_LINK_TOTAL_BUDGET_S <= 5.0


def test_short_link_total_budget_is_shared_across_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(P.time, "monotonic", lambda: clock["t"])
    timeouts: list[float] = []
    final = "https://www.linkedin.com/posts/a-b-activity-1-x/"
    hops = iter(["https://lnkd.in/p/second", "https://lnkd.in/p/third", final])

    class Resp:
        status_code = 302

        def __init__(self) -> None:
            self.headers = {"location": next(hops)}

    class FakeClient:
        def __init__(self, **kw: Any) -> None:
            t = kw["timeout"]
            timeouts.append(float(t) if isinstance(t, (int, float)) else float(t.read))

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def head(self, url: str, **kw: Any) -> Resp:
            clock["t"] += 3.0
            return Resp()

    monkeypatch.setattr(P.httpx, "Client", FakeClient)
    # hop 1 gets <=5 s and burns 3; hop 2 gets only the ~2 s that remain and burns 3 -> budget gone, no hop 3
    assert P._fetch_location("https://lnkd.in/p/first") is None
    assert len(timeouts) == 2 and timeouts[0] <= 5.0 and timeouts[1] <= 2.0 + 1e-6


def test_searcher_hands_the_account_proxy_to_permalink_capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    handle = AccountHandle(AccountSpec(id=4, proxy_url="http://p.example:8080", user_agent="UA/9"))
    searcher = _open_searcher(tmp_path, account=handle)
    searcher._card_raws = {"ck:KEY0": _raw(0)}
    got: dict[str, Any] = {}

    def fake_capture(page: Any, raws: Any, **kw: Any) -> P.PermalinkStats:
        got.update(kw)
        return P.PermalinkStats()

    monkeypatch.setattr(search_module, "capture_permalinks", fake_capture)
    searcher.capture_links(["ck:KEY0"])
    assert got["proxy_url"] == "http://p.example:8080" and got["user_agent"] == "UA/9"


# ------------------------------------------------------------------ #3 breaker
def test_three_consecutive_scrape_errors_stop_the_account_as_rate_limited(tmp_path: Any) -> None:
    searcher = _open_searcher(tmp_path)

    def boom(keyword: str, max_posts: int) -> Any:
        raise ScrapeError("HTTP 500")

    searcher._scan_keyword = boom  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(ScrapeError):
            searcher.scan("a", 5)
    with pytest.raises(AccountSafetyStop) as stop:
        searcher.scan("b", 5)
    assert stop.value.kind == "rate_limited" and not searcher.is_open  # browser closed first


def test_a_successful_scan_resets_the_error_streak(tmp_path: Any) -> None:
    searcher = _open_searcher(tmp_path)
    outcomes = iter(["err", "err", "ok", "err", "err", "ok"])

    def scan_keyword(keyword: str, max_posts: int) -> Any:
        if next(outcomes) == "err":
            raise ScrapeError("x")
        return "result"

    searcher._scan_keyword = scan_keyword  # type: ignore[method-assign]
    for _ in range(6):
        try:
            searcher.scan("k", 5)
        except ScrapeError:
            pass
    assert searcher.is_open


def test_same_site_403_on_background_api_is_a_rate_limit_but_a_document_403_is_not() -> None:
    assert is_rate_limit_response(BASE + "/voyager/api/feed/updates?x=1", 403, BASE)
    assert is_rate_limit_response(BASE + "/voyager/api/graphql?q=x", 429, BASE)
    assert not is_rate_limit_response(BASE + "/search/results/content/?keywords=x", 403, BASE)  # http_error, a keyword error
    assert not is_rate_limit_response("https://evil.example/voyager/api/x", 403, BASE)
    assert not is_rate_limit_response(BASE + "/voyager/api/x", 200, BASE)


# ------------------------------------------------------------------ #8 page budget
def test_the_page_load_budget_stops_further_passes_and_is_never_exceeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    settings = _settings(tmp_path, scrape_date_filters="past-24h,past-week,past-month", scrape_max_passes_per_keyword=3)
    searcher = LinkedInSearcher(settings, _pacer(), page_load_budget=2)
    searcher._stack, searcher._context, searcher._page = ExitStack(), MagicMock(), MagicMock()
    searcher._page.url = BASE + "/search/results/content/"
    loads: list[str] = []

    def fake_goto_pass(self: LinkedInSearcher, page: Any, keyword: str, date_filter: str, url: str, target: int, what: str) -> Any:
        self._page_loads += 1
        loads.append(url)
        return search_module._PassOutcome([], 0, 0, 0, True, "no_growth")

    monkeypatch.setattr(LinkedInSearcher, "_scan_pass", fake_goto_pass)
    result = searcher.scan("ai workshop", 80)
    assert len(loads) == 2 and searcher.page_loads == 2
    assert result.stop_reason == "page_budget"
    assert getattr(result, "page_loads", 2) == 2


def test_an_exhausted_budget_loads_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    searcher = LinkedInSearcher(_settings(tmp_path), _pacer(), page_load_budget=0)
    searcher._stack, searcher._context, searcher._page = ExitStack(), MagicMock(), MagicMock()
    called: list[int] = []
    monkeypatch.setattr(LinkedInSearcher, "_scan_pass", lambda *a, **k: called.append(1))
    result = searcher.scan("kw", 5)
    assert not called and result.stop_reason == "page_budget" and result.posts == [] and searcher.page_loads == 0


def test_every_goto_in_a_pass_is_counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    searcher = _open_searcher(tmp_path)
    page = MagicMock()
    page.url = BASE + "/search/results/content/"
    monkeypatch.setattr(searcher, "_checkpoint", lambda *a, **k: {"cards_found": 0, "no_results": True})
    searcher._scan_pass(page, "k", "past-week", BASE + "/search/results/content/?keywords=k", 5, " for 'k'")
    assert searcher.page_loads == 1


# ------------------------------------------------------------------ #7 expand budget per pass
def test_the_see_more_budget_is_per_pass_not_shared(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    searcher = _open_searcher(tmp_path)
    page = MagicMock()
    page.url = BASE + "/search/results/content/"
    monkeypatch.setattr(searcher, "_checkpoint", lambda *a, **k: {"cards_found": 3, "no_results": False})
    monkeypatch.setattr(search_module, "extract_raw_cards", lambda p: [])
    budgets: list[int] = []

    def fake_expand(p: Any, n: int, what: str, budget: int) -> int:
        budgets.append(budget)
        return budget  # spend it all

    monkeypatch.setattr(searcher, "_expand", fake_expand)
    for _ in range(3):
        searcher._scan_pass(page, "k", "past-week", BASE + "/s", 3, "")
    assert budgets == [searcher._settings.scrape_expand_max_per_keyword] * 3


# ------------------------------------------------------------------ #14 scrolling
def test_scroll_steps_are_several_eased_randomised_steps_summing_to_the_total() -> None:
    plans = [Pacer(3, 9, random.Random(seed), lambda s: None).scroll_steps(1000) for seed in range(30)]
    for plan in plans:
        assert 3 <= len(plan) <= 7 and abs(sum(d for d, _ in plan) - 1000) <= len(plan)
        assert all(d > 0 and 0 < w < 160 for d, w in plan)  # one wheel burst: < the pages' ~200 ms gesture debounce
        mid = plan[len(plan) // 2][0]
        assert mid >= plan[0][0] and mid >= plan[-1][0]  # eased: slow start and end
    waits = [w for plan in plans for _, w in plan]
    assert len(set(waits)) > len(waits) * 0.5  # no uniform fixed waits


def test_compact_scroll_steps_for_the_fixture_stay_in_one_gesture() -> None:
    for seed in range(20):
        plan = Pacer(3, 9, random.Random(seed), lambda s: None).scroll_steps(1000, compact=True)
        assert len(plan) == 3 and sum(w for _, w in plan) < 80 and abs(sum(d for d, _ in plan) - 1000) <= 3


def test_the_wheel_is_issued_in_multiple_steps_with_waits_between(tmp_path: Any) -> None:
    searcher = _open_searcher(tmp_path)
    page = MagicMock()
    searcher._wheel(page, 1000)
    calls = page.mouse.wheel.call_args_list
    assert len(calls) >= 3 and abs(sum(c.args[1] for c in calls) - 1000) <= len(calls)
    assert page.wait_for_timeout.call_count >= len(calls) - 1


def test_permalink_center_waits_are_randomised_not_fixed() -> None:
    waits: list[float] = []
    for seed in range(12):
        page = MagicMock()
        page.wait_for_timeout.side_effect = waits.append
        P._center(page, MagicMock(), Pacer(3, 9, random.Random(seed), lambda s: None))
    assert 700 not in waits and 250 not in waits and len(set(waits)) > 12


# ------------------------------------------------------------------ #18 stall logic
class _ClockPage:
    """wait_for_timeout advances a fake clock; ``grow_at`` is the clock time cards appear."""

    def __init__(self, clock: dict[str, float], grow_at: float | None) -> None:
        self.clock, self.grow_at = clock, grow_at

    def wait_for_timeout(self, ms: float) -> None:
        self.clock["t"] += ms / 1000


def _stall_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, grow_at: float | None) -> tuple[LinkedInSearcher, Any, dict[str, float]]:
    clock = {"t": 0.0}
    monkeypatch.setattr(search_module.time, "monotonic", lambda: clock["t"])
    page = _ClockPage(clock, grow_at)
    monkeypatch.setattr(search_module, "count_cards", lambda p: 6 if (grow_at is not None and clock["t"] >= grow_at) else 5)
    return _open_searcher(tmp_path), page, clock


def test_a_slow_network_first_idle_scroll_waits_up_to_twice_the_settle_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    searcher, page, clock = _stall_setup(monkeypatch, tmp_path, grow_at=3.9)  # settle is 2.5 s; cards at 3.9 s
    assert searcher._wait_for_more_cards(page, 5, extend=False) is False
    clock["t"] = 0.0
    assert searcher._wait_for_more_cards(page, 5, extend=True) is True  # found inside the extended window
    assert 3.9 <= clock["t"] <= 5.3


def test_a_genuine_end_of_results_waits_at_most_twice_the_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    searcher, page, clock = _stall_setup(monkeypatch, tmp_path, grow_at=None)
    assert searcher._wait_for_more_cards(page, 5, extend=True) is False
    settle = search_module.scroll_settle_ms(searcher._settings) / 1000
    assert settle <= clock["t"] <= 2 * settle + 0.5


def test_slow_growth_in_a_pass_raises_the_idle_threshold_for_later_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    searcher = _open_searcher(tmp_path)
    page = MagicMock()
    page.url = BASE + "/search/results/content/"
    state = {"cards": 3, "scrolls": 0}
    extras = lambda: {"cards_found": state["cards"], "no_results": False}  # noqa: E731
    monkeypatch.setattr(searcher, "_checkpoint", lambda *a, **k: extras())
    monkeypatch.setattr(search_module, "extract_raw_cards", lambda p: [])
    monkeypatch.setattr(searcher, "_expand", lambda *a, **k: 0)
    monkeypatch.setattr(searcher, "_wheel", lambda p, d: None)
    monkeypatch.setattr(searcher, "_hover_scroll_column", lambda p: None)
    waits: list[bool] = []

    def wait(p: Any, before: int, extend: bool = False) -> bool:
        waits.append(extend)
        searcher._last_wait_s = 2.0  # slow: > half the 2.5 s window
        state["scrolls"] += 1
        if state["scrolls"] == 1:  # pass 1: one slow growth
            state["cards"] += 1
            return True
        return False

    monkeypatch.setattr(searcher, "_wait_for_more_cards", wait)
    searcher._settings = _settings(tmp_path, scrape_no_growth_scrolls=1, scrape_max_scrolls=10)
    searcher._scan_pass(page, "k", "f", BASE + "/s", 99, "")
    state["scrolls"] = 1  # pass 2: never grows
    waits.clear()
    searcher._scan_pass(page, "k", "f", BASE + "/s", 99, "")
    assert len(waits) == 2  # no_growth_scrolls (1) + 1 extra because an earlier pass grew slowly
    assert waits[0] is True  # the first idle scroll gets the extended wait

"""LinkedInSearcher on real headless Chromium against the fixture corpus."""

from __future__ import annotations

import random
import re
import unicodedata
from datetime import timedelta

import pytest

from sales0.clock import local_today
from sales0.config import Settings
from sales0.scraper.errors import AccountSafetyStop, ScrapeError
from sales0.scraper.pacing import Pacer
from sales0.scraper.search import LinkedInSearcher
from sales0.scraper.urls import base_url

pytestmark = pytest.mark.browser

URN = "urn:li:activity:73000000000000000{:02d}"


def _norm(text: str) -> str:
    text = re.sub(r"#\w+", " ", unicodedata.normalize("NFKC", text).lower())
    return " ".join(text.split())


class RecordingPacer(Pacer):
    def __init__(self, settings: Settings) -> None:
        self.slept: list[float] = []
        super().__init__(settings.scrape_delay_min_s, settings.scrape_delay_max_s, random.Random(3), self.slept.append)


def test_run1_keywords_yield_nine_posts_with_the_expected_duplicates(connected_settings: Settings) -> None:
    pacer = RecordingPacer(connected_settings)
    base = base_url(connected_settings)
    today = local_today(connected_settings.tz)
    with LinkedInSearcher(connected_settings, pacer) as searcher:
        corp = searcher.search("corporate ai training", 15)
        work = searcher.search("ai workshop", 15)
        cons = searcher.search("ai consulting", 15)

    assert [p.urn for p in corp.posts] == [URN.format(n) for n in (1, 2, 3)]
    assert [p.urn for p in work.posts] == [URN.format(n) for n in (1, 5, 6, 7)]
    assert [p.urn for p in cons.posts] == [URN.format(n) for n in (8, 9)]
    assert sum(len(r.posts) for r in (corp, work, cons)) == 9
    for result in (corp, work, cons):
        assert result.parse_errors == 0 and result.drift_warning is False and result.scrolls <= 1

    p1 = corp.posts[0]
    assert p1.author_name == "Priya Menon"
    assert p1.author_headline == "Head of L&D at Acme Logistics"
    assert p1.author_profile_url == f"{base}/in/priya-menon-acme/"
    assert p1.posted_relative == "1d" and p1.posted_date == today - timedelta(days=1)
    assert p1.url == f"{base}/feed/update/{URN.format(1)}/"
    # The email sits after the "see more" fold: proves the toggle was expanded.
    assert "training@acmelogistics.example" in p1.text and "+91 98200 11111" in p1.text
    assert "see more" not in p1.text
    assert work.posts[0] == p1  # URN duplicate: identical post under a second keyword

    p5 = work.posts[1]
    assert re.search(r"[ऀ-ॿ]", p5.text) and "🤖" in p5.text and "arjun.rao@zentrix.example" in p5.text
    p2, p7 = corp.posts[1], work.posts[3]
    assert p2.urn != p7.urn and _norm(p2.text) == _norm(p7.text)  # content duplicate survives scraping
    assert "\n" in p2.text  # line structure preserved

    assert pacer.slept, "live-style pacing must be applied between page actions"
    assert all(connected_settings.scrape_delay_min_s <= s <= connected_settings.scrape_delay_max_s for s in pacer.slept)
    assert len(pacer.slept) >= 3  # at least one paced navigation per keyword


def test_per_keyword_cap_limits_posts(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        assert len(searcher.search("ai workshop", 2).posts) == 2
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD", "1")
    from sales0.config import get_settings, reset_settings

    reset_settings()
    capped = get_settings()
    with LinkedInSearcher(capped) as searcher:
        assert [p.urn for p in searcher.search("ai workshop", 15).posts] == [URN.format(1)]  # config cap wins


def test_zero_results_is_empty_without_a_drift_warning(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        result = searcher.search("quantum basket weaving", 15)
    assert (result.posts, result.drift_warning, result.parse_errors) == ([], False, 0)


def test_logged_in_page_without_cards_or_marker_flags_layout_drift(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        result = searcher.search("layout-drift-test", 15)
        assert searcher.is_open  # a warning, not a stop
        assert len(searcher.search("ai consulting", 15).posts) == 2
    assert result.posts == [] and result.drift_warning is True


@pytest.mark.parametrize(
    ("keyword", "kind", "url_part"),
    [
        ("checkpoint-test", "checkpoint", "/checkpoint/challenge/"),
        ("captcha-test", "captcha", "/search/results/content/"),
        ("ratelimit-test", "rate_limited", "/search/results/content/"),
    ],
)
def test_safety_pages_stop_the_run_and_close_the_browser(
    connected_settings: Settings, keyword: str, kind: str, url_part: str
) -> None:
    searcher = LinkedInSearcher(connected_settings)
    with searcher:
        assert len(searcher.search("ai consulting", 15).posts) == 2
        with pytest.raises(AccountSafetyStop) as stop:
            searcher.search(keyword, 15)
        assert stop.value.kind == kind and url_part in stop.value.url
        assert searcher.is_open is False  # closed immediately, not at __exit__
        with pytest.raises(ScrapeError):  # nothing further can be searched after a stop
            searcher.search("ai workshop", 15)


def test_a_logged_out_profile_stops_on_enter(fixture_settings: Settings) -> None:
    searcher = LinkedInSearcher(fixture_settings)
    with pytest.raises(AccountSafetyStop) as stop:
        searcher.__enter__()
    assert stop.value.kind == "logout"
    assert searcher.is_open is False
    assert searcher.session is not None and searcher.session.reason == "no_cookie"


def test_a_session_lost_mid_run_stops_with_logout(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        assert searcher.search("ai consulting", 15).posts
        searcher._context.clear_cookies()  # type: ignore[union-attr]  # simulates LinkedIn ending the session
        with pytest.raises(AccountSafetyStop) as stop:
            searcher.search("ai workshop", 15)
    assert stop.value.kind == "logout" and "/authwall" in stop.value.url


def test_search_outside_the_context_manager_is_refused(connected_settings: Settings) -> None:
    with pytest.raises(ScrapeError):
        LinkedInSearcher(connected_settings).search("ai workshop", 5)


def test_blank_keyword_is_rejected_before_any_request(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        with pytest.raises(ValueError):
            searcher.search("   ", 5)
        assert searcher.is_open


class ActionLog:
    """One ordered log of paced delays and the raw page actions they must precede."""

    def __init__(self) -> None:
        self.events: list[str] = []


class LoggingPacer(Pacer):
    def __init__(self, settings: Settings, log: ActionLog) -> None:
        super().__init__(
            settings.scrape_delay_min_s, settings.scrape_delay_max_s, random.Random(5), lambda _s: log.events.append("delay")
        )


class LoggingSearcher(LinkedInSearcher):
    def __init__(self, settings: Settings) -> None:
        self.log = ActionLog()
        super().__init__(settings, LoggingPacer(settings, self.log))

    def _wheel(self, page, delta_y):  # type: ignore[no-untyped-def]
        self.log.events.append("scroll")
        super()._wheel(page, delta_y)

    def _click(self, button):  # type: ignore[no-untyped-def]
        self.log.events.append("expand")
        super()._click(button)


def test_lazy_results_load_across_scrolls_and_each_action_is_preceded_by_a_delay(connected_settings: Settings) -> None:
    with LoggingSearcher(connected_settings) as searcher:
        result = searcher.search("lazy-scroll-test", 15)
        events = searcher.log.events

    # Cards arrive ~700ms after each scroll (slower than the old fixed 500ms wait): polling picks them up.
    assert [p.urn for p in result.posts] == [URN.format(n) for n in (1, 5, 6, 7)]
    assert result.scrolls == 4  # 3 scrolls that loaded a card + 1 that found the end (= max_scrolls)
    assert "training@acmelogistics.example" in result.posts[0].text  # expanded after lazy loading
    assert events.count("scroll") == 4 and events.count("expand") == 4  # all four posts are long
    for i, event in enumerate(events):
        if event in ("scroll", "expand"):
            assert i > 0 and events[i - 1] == "delay", f"{event} at {i} not preceded by a delay: {events}"
    assert events[0] == "delay"  # the navigation is paced too


def test_background_rate_limit_response_stops_the_run(connected_settings: Settings) -> None:
    searcher = LinkedInSearcher(connected_settings)
    with searcher:
        assert len(searcher.search("ai consulting", 15).posts) == 2
        with pytest.raises(AccountSafetyStop) as stop:
            searcher.search("ratelimit-scroll-test", 15)  # the page itself is 200; its voyager XHR is 429
        assert stop.value.kind == "rate_limited"
        assert "/voyager/api/" in stop.value.url
        assert searcher.is_open is False
        with pytest.raises(ScrapeError):  # no further keyword runs
            searcher.search("ai workshop", 15)


def test_a_checkpoint_on_a_later_page_load_mid_scroll_stops_the_run(connected_settings: Settings) -> None:
    searcher = LoggingSearcher(connected_settings)
    with searcher:
        with pytest.raises(AccountSafetyStop) as stop:
            searcher.search("checkpoint-scroll-test", 15)
        assert stop.value.kind == "checkpoint" and "/checkpoint/challenge/" in stop.value.url
        assert searcher.log.events.count("scroll") == 2  # the 1st scroll loaded more; the 2nd hit the checkpoint
        assert searcher.is_open is False
        with pytest.raises(ScrapeError):
            searcher.search("ai workshop", 15)

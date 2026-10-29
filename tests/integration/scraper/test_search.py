"""LinkedInSearcher on real headless Chromium against the fixture corpus.

Phase 1.2: results pages are LinkedIn's SDUI markup (no URN, no post link anywhere in the DOM;
obfuscated classes). Posts are identified by their ``componentkey`` (``ck:<KEY>``) unless
"Copy link to post" yields the URN. ``legacy-urn-test`` keeps the Phase 1.1 URN card shapes
(data-urn / data-id / /feed/update/ / /posts/) for the fallback strategy."""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest

from sales0.clock import local_today
from sales0.config import Settings
from sales0.linkedin_fixture.corpus import DRIFT_KEYWORD, LAZY_KEYWORD, LEGACY_KEYWORD, FixturePost, load_corpus, posts_for
from sales0.scraper import selectors as S
from sales0.scraper import search as search_module
from sales0.scraper.errors import AccountSafetyStop, ScrapeError, SessionMissing
from sales0.scraper.pacing import Pacer
from sales0.scraper.search import SCROLL_TARGET_JS, LinkedInSearcher
from sales0.scraper.types import KeywordScrapeResult, SessionCheckResult
from sales0.scraper.urls import base_url, permalink, search_url

pytestmark = pytest.mark.browser

URN = "urn:li:activity:73000000000000000{:02d}"


def expected_ref(post: FixturePost, keyword: str) -> str:
    """The ref the default run assigns: the URN when "Copy link to post" yields it, else ck:<KEY>."""
    return post.urn if post.copy_link == "urn" else f"ck:{post.key_for(keyword)}"


def assert_refs_and_links(result: KeywordScrapeResult, keyword: str, base: str) -> None:
    corpus = posts_for(keyword)
    assert [p.source_ref for p in result.posts] == [expected_ref(c, keyword) for c in corpus]
    for post, fx in zip(result.posts, corpus, strict=True):
        assert post.source_ref in fx.possible_refs()
        if fx.copy_link == "urn":
            assert (post.urn, post.link_kind, post.url) == (fx.urn, "post", permalink(base, fx.urn))
        else:
            assert (post.urn, post.link_kind) == (None, "profile")
            assert post.url == post.author_profile_url == f"{base}/{fx.profile_path}/"
    assert result.permalinks_found == sum(1 for c in corpus if c.copy_link == "urn")
    assert result.permalink_attempts == sum(1 for c in corpus if c.copy_link != "none")


def _norm(text: str) -> str:
    text = re.sub(r"#\w+", " ", unicodedata.normalize("NFKC", text).lower())
    return " ".join(text.split())


class RecordingPacer(Pacer):
    def __init__(self, settings: Settings) -> None:
        self.slept: list[float] = []
        super().__init__(settings.scrape_delay_min_s, settings.scrape_delay_max_s, random.Random(3), self.slept.append)


@pytest.fixture
def request_log(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every request URL made by the searcher's browser context (wraps the real open_context)."""
    urls: list[str] = []
    real = search_module.open_context

    @contextmanager
    def recording(settings: Settings, *, headless: bool):  # type: ignore[no-untyped-def]
        with real(settings, headless=headless) as context:
            context.on("request", lambda req: urls.append(req.url))
            yield context

    monkeypatch.setattr(search_module, "open_context", recording)
    yield urls


def test_run1_keywords_yield_nine_sdui_posts_identified_by_key_or_copied_urn(connected_settings: Settings) -> None:
    pacer = RecordingPacer(connected_settings)
    base = base_url(connected_settings)
    today = local_today(connected_settings.tz)
    keywords = ("corporate ai training", "ai workshop", "ai consulting")
    with LinkedInSearcher(connected_settings, pacer) as searcher:
        corp, work, cons = (searcher.search(k, 15) for k in keywords)

    assert sum(len(r.posts) for r in (corp, work, cons)) == 9
    for result, keyword in zip((corp, work, cons), keywords, strict=True):
        assert result.strategy == "sdui"
        assert_refs_and_links(result, keyword, base)
        assert result.parse_errors == 0 and result.drift_warning is False and result.scrolls <= 1
        assert result.field_misses == {} and result.no_results is False
        assert result.cards_found == len(result.posts)
        url = search_url(base, keyword, connected_settings.scrape_date_filter)
        assert all(p.search_url == url for p in result.posts)
    assert [p.urn for p in corp.posts] == [None, URN.format(2), None]  # P2's menu copies its share link
    assert [p.urn for p in cons.posts] == [URN.format(8), None]

    p1 = corp.posts[0]
    assert p1.author_name == "Priya Menon"
    assert p1.author_headline == "Head of L&D at Acme Logistics"
    assert p1.author_profile_url == f"{base}/in/priya-menon-acme/"
    assert p1.posted_relative == "1d" and p1.posted_date == today - timedelta(days=1)
    assert p1.url == p1.author_profile_url and p1.link_kind == "profile"  # its menu copies nothing
    # The email sits after the "…more" fold: proves the toggle was found and expanded.
    assert "training@acmelogistics.example" in p1.text and "+91 98200 11111" in p1.text
    assert not p1.text.rstrip().lower().endswith("more") and not p1.text.rstrip().endswith("…")
    w1 = work.posts[0]  # the same post under a second keyword: its key changed, the content did not
    assert w1.source_ref != p1.source_ref and w1.text == p1.text and w1.author_name == p1.author_name

    p5 = work.posts[1]
    assert re.search(r"[ऀ-ॿ]", p5.text) and "🤖" in p5.text and "arjun.rao@zentrix.example" in p5.text
    assert p5.author_name == "Arjun Rao" and p5.author_headline == "CTO at Zentrix Retail"
    assert p5.link_kind == "post" and p5.url == permalink(base, URN.format(5))
    p2, p7 = corp.posts[1], work.posts[3]
    assert p2.source_ref != p7.source_ref and _norm(p2.text) == _norm(p7.text)  # content duplicate survives
    assert "\n" in p2.text  # line structure preserved
    p8 = cons.posts[0]
    assert (p8.author_name, p8.author_headline) == ("Sanjay Gupta", "COO at Meridian Insurance")
    assert "it-projects@meridian.example" in p8.text

    assert pacer.slept, "live-style pacing must be applied between page actions"
    assert len(pacer.slept) >= 3  # at least one paced navigation per keyword
    short = (connected_settings.scrape_delay_min_s / 4, connected_settings.scrape_delay_max_s / 4)
    assert all(
        connected_settings.scrape_delay_min_s <= s <= connected_settings.scrape_delay_max_s or short[0] <= s <= short[1]
        for s in pacer.slept
    )


def test_the_run2_keyword_parses_a_person_and_a_company_author(connected_settings: Settings) -> None:
    base = base_url(connected_settings)
    with LinkedInSearcher(connected_settings) as searcher:
        result = searcher.search("claude code training", 15)
    assert result.strategy == "sdui"
    assert_refs_and_links(result, "claude code training", base)
    p10, p11 = result.posts
    assert p10.urn == URN.format(10)  # copied link: /posts/<slug>-activity-<id>-… → URN
    assert (p10.author_name, p10.author_headline) == ("Anita Desai", "Engineering Manager at QuantaWorks")
    assert "lead@quantaworks.example" in p10.text
    assert p11.author_name == "Briltus Academy" and "hr@briltus.example" in p11.text
    assert p11.url.startswith(f"{base}/company/") and p11.link_kind == "profile"
    assert result.field_misses == {} and result.parse_errors == 0


def test_legacy_urn_layout_still_parses_through_the_urn_strategy(connected_settings: Settings) -> None:
    base = base_url(connected_settings)
    with LinkedInSearcher(connected_settings) as searcher:
        result = searcher.search(LEGACY_KEYWORD, 15)
    corpus = load_corpus()
    assert result.strategy == "urn"
    assert [p.source_ref for p in result.posts] == [p.urn for p in corpus] and len(result.posts) == 10
    assert all(p.link_kind == "post" and p.url == permalink(base, p.source_ref) for p in result.posts)
    assert (result.permalink_attempts, result.permalinks_found) == (0, 0)
    assert result.drift_warning is False and result.parse_errors == 0


def test_per_keyword_cap_limits_posts(connected_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        assert len(searcher.search("ai workshop", 2).posts) == 2
    monkeypatch.setenv("SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD", "1")
    from sales0.config import get_settings, reset_settings

    reset_settings()
    capped = get_settings()
    with LinkedInSearcher(capped) as searcher:
        p1 = posts_for("ai workshop")[0]
        assert [p.source_ref for p in searcher.search("ai workshop", 15).posts] == [f"ck:{p1.key_for('ai workshop')}"]


def test_zero_results_text_marker_is_empty_without_a_drift_warning(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        result = searcher.search("quantum basket weaving", 15)
    assert (result.posts, result.drift_warning, result.parse_errors) == ([], False, 0)
    assert result.no_results is True and result.cards_found == 0  # text-only "No results found", no legacy class


def test_sdui_listitems_without_posts_flag_layout_drift(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        result = searcher.search(DRIFT_KEYWORD, 15)
        assert searcher.is_open  # a warning, not a stop
        assert len(searcher.search("ai consulting", 15).posts) == 2
    assert result.posts == [] and result.drift_warning is True and result.strategy == "none"
    assert result.no_results is False and result.cards_found == 3  # the listitems are counted
    assert (result.permalink_attempts, result.permalinks_found) == (0, 0)


def test_an_http_error_on_the_search_document_fails_only_that_keyword(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        searcher._context.route(  # type: ignore[union-attr]
            re.compile(r".*/search/results/content/\?keywords=server-error.*"),
            lambda route: route.fulfill(status=500, body="<html><body><main>Oops</main></body></html>",
                                        content_type="text/html"),
        )
        with pytest.raises(ScrapeError, match="HTTP 500"):
            searcher.search("server-error", 15)
        assert searcher.is_open
        assert len(searcher.search("ai consulting", 15).posts) == 2


@pytest.mark.parametrize(
    ("keyword", "kind", "url_part"),
    [
        ("checkpoint-test", "checkpoint", "/checkpoint/challenge/"),
        ("captcha-test", "captcha", "/search/results/content/"),
        ("ratelimit-test", "rate_limited", "/search/results/content/"),
        ("logout-test", "logout", "/authwall"),  # cookie still present; the redirect path says logged out
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


def test_a_profile_without_a_session_cookie_raises_session_missing_without_any_request(
    fixture_settings: Settings, request_log: list[str]
) -> None:
    searcher = LinkedInSearcher(fixture_settings)
    with pytest.raises(SessionMissing) as missing:
        searcher.__enter__()
    assert not isinstance(missing.value, (AccountSafetyStop, ScrapeError))
    assert missing.value.url.endswith("/__fixtures/linkedin/feed/")
    assert searcher.is_open is False
    assert searcher.session is not None and searcher.session.reason == "no_cookie"
    base = base_url(fixture_settings)
    assert [u for u in request_log if u.startswith(base)] == [], "no LinkedIn page may be opened without a cookie"


def test_a_session_lost_between_keywords_stops_with_logout_before_the_next_search(
    connected_settings: Settings, request_log: list[str]
) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        assert searcher.search("ai consulting", 15).posts
        searcher._context.clear_cookies()  # type: ignore[union-attr]  # simulates LinkedIn ending the session
        before = len(request_log)
        with pytest.raises(AccountSafetyStop) as stop:
            searcher.search("ai workshop", 15)
    assert stop.value.kind == "logout"
    assert "keywords=ai%20consulting" in stop.value.url  # caught at the pre-keyword checkpoint (rule: no_cookie)
    assert not any("keywords=ai%20workshop" in u for u in request_log[before:]), "the next search was never opened"


def test_search_outside_the_context_manager_is_refused(connected_settings: Settings) -> None:
    with pytest.raises(ScrapeError):
        LinkedInSearcher(connected_settings).search("ai workshop", 5)


def test_blank_keyword_is_rejected_before_any_request(connected_settings: Settings) -> None:
    with LinkedInSearcher(connected_settings) as searcher:
        with pytest.raises(ValueError):
            searcher.search("   ", 5)
        assert searcher.is_open


@pytest.mark.parametrize(
    ("reason", "raises", "kind"),
    [
        ("no_cookie", SessionMissing, None),
        ("redirected_login", AccountSafetyStop, "logout"),
        ("checkpoint", AccountSafetyStop, "checkpoint"),
        ("captcha", AccountSafetyStop, "captcha"),
        ("rate_limited", AccountSafetyStop, "rate_limited"),
        ("error", ScrapeError, None),
    ],
)
def test_run_start_check_maps_every_failed_session_reason(
    reason: str, raises: type[Exception], kind: str | None
) -> None:
    searcher = LinkedInSearcher(Settings(linkedin_mode="live"))
    url = "https://www.linkedin.com/feed/"
    with pytest.raises(raises) as exc:
        searcher._enter_verdict(SessionCheckResult(logged_in=False, reason=reason, url=url))  # type: ignore[arg-type]
    if kind is not None:
        assert exc.value.kind == kind and exc.value.url == url  # type: ignore[attr-defined]
    searcher._enter_verdict(SessionCheckResult(logged_in=True, reason="ok", url=url))  # ok passes


class ActionLog:
    """One ordered log of paced delays and the raw page actions they must precede."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.hovers = 0


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

    def _hover_scroll_column(self, page):  # type: ignore[no-untyped-def]
        self.log.hovers += 1  # not an event: the hover belongs to the scroll it precedes
        super()._hover_scroll_column(page)


def test_lazy_results_load_across_scrolls_and_each_action_is_preceded_by_a_delay(connected_settings: Settings) -> None:
    with LoggingSearcher(connected_settings) as searcher:
        result = searcher.search(LAZY_KEYWORD, 15)
        events = searcher.log.events
        # The wheel goes over the SDUI lazy column: its visible centre is a hover target.
        point = searcher._page.evaluate(SCROLL_TARGET_JS, S.SDUI_SCROLL_CONTAINER)  # type: ignore[union-attr]
        assert point and point["x"] > 0 and point["y"] > 0
        assert searcher.log.hovers == 4

    # Cards arrive ~700ms after each scroll (slower than a fixed short wait): polling picks them up.
    assert result.strategy == "sdui" and len(result.posts) == 4
    assert [p.source_ref for p in result.posts] == [expected_ref(c, LAZY_KEYWORD) for c in posts_for("ai workshop")]
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

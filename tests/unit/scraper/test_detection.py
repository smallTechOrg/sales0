import pytest

from sales0.scraper.detection import PageSignals, classify


def _sig(url: str, status: int | None = 200, captcha: bool = False, logged_in: bool = True) -> PageSignals:
    return PageSignals(url=url, status=status, has_captcha=captcha, has_logged_in_marker=logged_in)


@pytest.mark.parametrize("status", [429, 999])
def test_classify_flags_rate_limit_statuses(status: int) -> None:
    assert classify(_sig("https://www.linkedin.com/search/results/content/", status)) == "rate_limited"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/checkpoint/challenge/AgH",
        "http://127.0.0.1:1/__fixtures/linkedin/checkpoint/challenge/",
    ],
)
def test_classify_flags_checkpoint_urls(url: str) -> None:
    assert classify(_sig(url, logged_in=False)) == "checkpoint"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/authwall?trk=x",
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/uas/login?session_redirect=x",
        "https://www.linkedin.com/signup/cold-join",
        "http://127.0.0.1:1/__fixtures/linkedin/authwall",
    ],
)
def test_classify_flags_login_walls_as_logout(url: str) -> None:
    assert classify(_sig(url, logged_in=False)) == "logout"


def test_classify_flags_captcha_even_when_logged_in() -> None:
    assert classify(_sig("https://www.linkedin.com/search/results/content/", captcha=True)) == "captcha"


def test_classify_returns_none_for_a_normal_logged_in_page() -> None:
    assert classify(_sig("https://www.linkedin.com/feed/")) is None


def test_classify_does_not_mistake_a_keyword_containing_login_for_a_wall() -> None:
    url = "https://www.linkedin.com/search/results/content/?keywords=login%20checkpoint"
    assert classify(_sig(url)) is None


def test_classify_prefers_rate_limit_over_other_signals() -> None:
    assert classify(_sig("https://www.linkedin.com/checkpoint/x", status=429, captcha=True)) == "rate_limited"


@pytest.mark.parametrize(
    ("url", "status", "base", "expected"),
    [
        ("https://www.linkedin.com/voyager/api/search/dash/clusters?q=x", 429, "https://www.linkedin.com", True),
        ("https://www.linkedin.com/voyager/api/graphql", 999, "https://www.linkedin.com", True),
        ("https://linkedin.com/feed/", 429, "https://www.linkedin.com", True),
        ("https://static.licdn.com/sc/h/x.js", 429, "https://www.linkedin.com", False),  # CDN, not the session
        ("https://evil-linkedin.com/x", 429, "https://www.linkedin.com", False),
        ("https://www.linkedin.com/voyager/api/graphql", 500, "https://www.linkedin.com", False),
        ("https://www.linkedin.com/voyager/api/graphql", 200, "https://www.linkedin.com", False),
        ("http://127.0.0.1:8123/__fixtures/linkedin/voyager/api/x", 429, "http://127.0.0.1:8123/__fixtures/linkedin", True),
        ("http://127.0.0.1:9999/other", 429, "http://127.0.0.1:8123/__fixtures/linkedin", False),
        ("data:text/plain,hi", 429, "https://www.linkedin.com", False),
    ],
)
def test_rate_limit_responses_count_only_from_the_linkedin_site(url: str, status: int, base: str, expected: bool) -> None:
    from sales0.scraper.detection import is_rate_limit_response

    assert is_rate_limit_response(url, status, base) is expected


def test_logged_in_marker_matches_real_linkedin_and_fixture_nav_markup() -> None:
    from sales0.scraper import selectors

    parts = {p.strip() for p in selectors.LOGGED_IN_MARKER.split(",")}
    assert {"#global-nav", "header.global-nav", "nav.global-nav__nav", "nav.global-nav"} <= parts

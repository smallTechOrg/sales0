"""Fixture LinkedIn routes, in-process, plus the live-mode 404 guarantee."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sales0.config import Settings
from sales0.linkedin_fixture import build_app
from sales0.scraper.urls import FIXTURE_PREFIX

P = FIXTURE_PREFIX


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), follow_redirects=False)


def _login(client: TestClient) -> None:
    response = client.get(f"{P}/login")
    assert response.status_code == 302
    assert response.headers["location"].endswith(f"{P}/feed/")
    assert "li_at=fixture-session" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()


def test_feed_requires_the_session_cookie(client: TestClient) -> None:
    response = client.get(f"{P}/feed/")
    assert response.status_code == 302 and response.headers["location"].endswith(f"{P}/authwall")
    wall = client.get(f"{P}/authwall")
    assert wall.status_code == 200 and "<nav class=\"global-nav\"" not in wall.text
    _login(client)
    feed = client.get(f"{P}/feed/")
    assert feed.status_code == 200 and '<nav class="global-nav"' in feed.text


def test_search_renders_matching_posts_in_corpus_order(client: TestClient) -> None:
    _login(client)
    response = client.get(f"{P}/search/results/content/", params={"keywords": " AI Workshop ", "datePosted": '"past-week"'})
    assert response.status_code == 200
    urns = [u for u in ("01", "05", "06", "07") if f'data-urn="urn:li:activity:73000000000000000{u}"' in response.text]
    assert urns == ["01", "05", "06", "07"]
    assert response.text.index("7300000000000000001") < response.text.index("7300000000000000007")
    assert "search-reusable-search-no-results" not in response.text


def test_search_with_zero_matches_renders_the_no_results_marker(client: TestClient) -> None:
    _login(client)
    response = client.get(f"{P}/search/results/content/", params={"keywords": "quantum basket weaving"})
    assert response.status_code == 200
    assert 'class="search-reusable-search-no-results"' in response.text
    assert "data-urn" not in response.text


def test_search_escapes_the_keyword(client: TestClient) -> None:
    _login(client)
    response = client.get(f"{P}/search/results/content/", params={"keywords": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in response.text


def test_search_without_cookie_redirects_to_the_authwall(client: TestClient) -> None:
    response = client.get(f"{P}/search/results/content/", params={"keywords": "ai workshop"})
    assert response.status_code == 302 and response.headers["location"].endswith(f"{P}/authwall")


def test_safety_trigger_keywords_produce_their_pages(client: TestClient) -> None:
    _login(client)
    checkpoint = client.get(f"{P}/search/results/content/", params={"keywords": " Checkpoint-Test"})
    assert checkpoint.status_code == 302 and checkpoint.headers["location"].endswith(f"{P}/checkpoint/challenge/")
    assert "security check" in client.get(f"{P}/checkpoint/challenge/").text
    captcha = client.get(f"{P}/search/results/content/", params={"keywords": "captcha-test"})
    assert captcha.status_code == 200 and 'src="' in captcha.text and "captcha" in captcha.text
    assert client.get(f"{P}/search/results/content/", params={"keywords": "ratelimit-test"}).status_code == 429


def test_post_permalink_serves_the_post_or_404(client: TestClient) -> None:
    _login(client)
    ok = client.get(f"{P}/feed/update/urn:li:activity:7300000000000000008/")
    assert ok.status_code == 200 and "Meridian Insurance" in ok.text
    assert client.get(f"{P}/feed/update/urn:li:activity:1/").status_code == 404


def test_fixture_routes_are_not_mounted_in_live_mode() -> None:
    from sales0.web.app import create_app

    live = TestClient(create_app(Settings(linkedin_mode="live")), follow_redirects=False)
    assert live.get(f"{P}/feed/").status_code == 404
    assert live.get(f"{P}/login").status_code == 404
    fixture = TestClient(create_app(Settings(linkedin_mode="fixture")), follow_redirects=False)
    assert fixture.get(f"{P}/feed/").status_code == 302

"""Shell (slice E): redirect, header light, alert banner, cost badge, nav, stub pages,
static assets and the error page — on a real migrated SQLite file."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.web.app import create_app
from tests.integration.web.conftest import HX, add_llm_call, set_session

STUBS = [
    ("/pipeline", "Pipeline", 2),
    ("/contacts", "Contacts", 2),
    ("/competitors", "Competitors &amp; Groups", 2),
    ("/import", "Import", 2),
    ("/metrics", "Metrics", 4),
    ("/whatsapp", "WhatsApp groups", 5),
]


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def test_root_redirects_to_the_review_inbox(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/inbox"
    assert "Review inbox" in client.get("/").text


def test_header_shows_not_connected_light_zero_cost_and_nav_in_order(client: TestClient) -> None:
    html = client.get("/inbox").text
    light = re.search(r'<a id="status-light"[^>]*class="([^"]+)"', html)
    assert light and "status-light--not-connected" in light.group(1)
    assert "LinkedIn not connected" in html
    assert re.search(r'id="cost-badge".*?AI today <strong>\$0\.00</strong> · month <strong>\$0\.00</strong>', html, re.S)
    nav = html.split('aria-label="Primary"', 1)[1].split("</nav>", 1)[0]
    labels = re.findall(r'<a href="(/[a-z]+)"', nav)
    assert labels == ["/inbox", "/runs", "/setup", "/pipeline", "/contacts", "/competitors", "/import", "/outreach", "/metrics", "/digest", "/suggestions", "/costs", "/schedule", "/whatsapp"]
    assert nav.count("badge--stub") == 6  # Outreach is real since Phase 3
    assert 'href="/inbox" aria-current="page"' in nav
    assert 'id="alert-banner"' not in html
    assert 'id="toasts"' in html and 'aria-live="polite"' in html
    assert "FIXTURE MODE" not in html


def test_connected_light_shows_last_run_age(client: TestClient) -> None:
    set_session("connected", last_successful_run_at=utcnow() - timedelta(hours=2, minutes=5))
    html = client.get("/runs").text
    assert "status-light--connected" in html
    assert "LinkedIn connected · last run 2h ago" in html


def test_needs_attention_shows_banner_on_every_page_and_red_light(client: TestClient) -> None:
    set_session("needs_attention", last_error="Checkpoint page at /checkpoint/challenge/")
    for path in ("/inbox", "/runs", "/setup", "/metrics"):
        html = client.get(path).text
        assert 'id="alert-banner"' in html, path
        assert 'role="alert"' in html
        assert "LinkedIn needs attention — Checkpoint page at /checkpoint/challenge/. Runs are paused to protect your account." in html
        assert "status-light--needs-attention" in html
        assert 'href="/setup#linkedin-card">Go to Setup</a>' in html
    banner = client.get("/_shell/banner", headers=HX)
    assert banner.status_code == 200 and 'id="alert-banner"' in banner.text
    light = client.get("/_shell/status-light", headers=HX).text
    assert light.lstrip().startswith('<a id="status-light"') and "LinkedIn needs attention" in light


def test_banner_fragment_is_empty_when_linkedin_is_fine(client: TestClient) -> None:
    set_session("connected")
    resp = client.get("/_shell/banner", headers=HX)
    assert resp.status_code == 200 and resp.text.strip() == ""


def test_cost_badge_sums_todays_ai_calls(client: TestClient) -> None:
    add_llm_call(12_300)
    add_llm_call(1_000)
    html = client.get("/inbox").text
    assert "AI today <strong>$0.0133</strong> · month <strong>$0.0133</strong>" in html


@pytest.mark.parametrize(("path", "title", "phase"), STUBS)
def test_stub_pages_are_labelled_coming_in_phase_n_with_no_controls(client: TestClient, path: str, title: str, phase: int) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    main = _main(resp.text)
    assert f"Coming in Phase {phase}" in main
    assert f"<h1 id=\"stub-title\">{title}</h1>" in main
    assert 'class="badge badge--stub"' in main
    assert "<form" not in main and "<button" not in main and "<input" not in main
    assert f'href="{path}" aria-current="page"' in resp.text


def test_fixture_mode_is_labelled_in_the_header(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "fixture")
    reset_settings()
    with TestClient(create_app()) as c:
        html = c.get("/inbox").text
    assert "FIXTURE MODE" in html


def test_static_assets_are_served_with_the_component_contract(client: TestClient) -> None:
    css = client.get("/static/css/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    for cls in (".page", ".card", ".card__header", ".btn--primary", ".badge--stub", ".tabs__tab--active", ".empty-state",
                ".toast--error", ".banner--alert", ".status-light--busy", ".bulk-bar", ".visually-hidden", ":focus-visible",
                "prefers-reduced-motion", "--font-size-base: 1rem"):
        assert cls in css.text, cls
    js = client.get("/static/js/app.js")
    assert js.status_code == 200 and "htmx.config.defaultSwapStyle" in js.text and "htmx:confirm" in js.text
    page = client.get("/inbox").text
    assert '<script src="/static/vendor/htmx.min.js" defer></script>' in page
    assert '<script src="/static/js/app.js" defer></script>' in page
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_unknown_page_renders_a_human_error_page_inside_the_shell(client: TestClient) -> None:
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert "That item no longer exists — refresh the page." in resp.text
    assert "Back to sales0" in resp.text and 'class="site-header"' in resp.text
    assert "Traceback" not in resp.text


def test_htmx_error_is_a_toast_retargeted_at_toasts(client: TestClient) -> None:
    resp = client.post("/inbox/decide", data={"decision": "accept"}, headers=HX)
    assert resp.status_code == 200
    assert resp.headers["HX-Retarget"] == "#toasts" and resp.headers["HX-Reswap"] == "beforeend"
    assert 'class="toast toast--error"' in resp.text
    assert "Choose Accept or Discard for at least one enquiry." in resp.text

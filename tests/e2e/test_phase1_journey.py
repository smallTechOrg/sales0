"""Phase-1 golden path, end to end (roadmap gate assertion 6), re-proven through the Phase 1.1
connect + detection + parsing paths (Phase 1.1 gate assertions 2 and 3).

Live ``python -m sales0`` (fixture LinkedIn, port 8766, temp SQLite) + headless Chromium +
the REAL Haiku key from ``.env``:

setup (keywords) → Connect LinkedIn (headless fixture window walks login → 2FA →
"remember this browser" → feed, and only then turns green) → Re-check (headless) → Run now
(live progress) → run 1 counters
→ inbox cards with real Haiku content + cost → accept P1 → bulk discard with a reason →
Discarded search → server restart: state survives → run 2 on the restarted server learns
from the decisions (few-shot) → server JSON log has llm_call + http_request lines.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from sales0.config import get_settings
from tests.e2e._flows import (
    LINK_LABELS,
    LOGIN_FLOW_PATHS,
    add_keyword,
    assert_server_logs_clean,
    card,
    connect_linkedin,
    fixture_post,
    recheck_linkedin,
    run_now,
    settled,
    tab_count,
)
from tests.support.live_server import LiveServer

RUN1_KEYWORDS = ["corporate ai training", "ai workshop", "ai consulting"]
RUN1_NEW = ["P1", "P2", "P3", "P5", "P6", "P8", "P9"]
AI_DISCARDS = ["P2", "P3", "P9"]
BULK_REASON = "Training agency / marketplace"
COST_TEXT = re.compile(r"^AI \$\d+\.\d{2,} · [\d.]+k? tok$")


def _llm_calls(server: LiveServer) -> list[dict]:
    return [e for e in server.log_events() if e.get("event") == "llm_call"]


def _request_paths(server: LiveServer) -> list[str]:
    return [str(e.get("path")) for e in server.log_events() if e.get("event") == "http_request"]


def _assert_connect_walked_the_2fa_flow(server: LiveServer) -> None:
    """The fixture login was walked step by step, and Connect turned green only after the feed."""
    events = server.log_events()
    paths = [(i, e.get("path")) for i, e in enumerate(events) if e.get("event") == "http_request"]
    positions: list[int] = []
    cursor = -1
    for step in LOGIN_FLOW_PATHS:
        hit = next((i for i, path in paths if path == step and i > cursor), None)
        assert hit is not None, f"Connect never requested {step} (after the previous step); saw {[p for _, p in paths]}"
        positions.append(hit)
        cursor = hit
    connected = [
        i
        for i, e in enumerate(events)
        if e.get("event") == "linkedin_session" and e.get("action") == "connect" and e.get("result") == "connected"
    ]
    assert len(connected) == 1, [e for e in events if e.get("event") == "linkedin_session"]
    remember_browser_at, feed_at = positions[-2], positions[-1]
    assert connected[0] > feed_at > remember_browser_at, "Connect turned green before the feed was reached"
    event = events[connected[0]]
    assert event["headless"] is True, event  # fixture-mode Connect is headless
    assert event["browser"] == "chromium", event  # the harness pins the bundled Chromium
    assert event["cookie_present"] is True and event["reason"] == "ok", event
    assert str(event["final_path"]).endswith("/feed/"), event
    assert str(event["verdict"]).split(":", 1)[0] == "ok", event


def test_phase1_journey_setup_connect_run_triage_decide_search_restart(server: LiveServer, ui: Page) -> None:
    page, base = ui, server.base_url

    # --- empty start: / lands on the Review inbox empty state, fixture mode is labelled
    page.goto(f"{base}/")
    expect(page).to_have_url(f"{base}/inbox")
    expect(page.get_by_role("heading", level=1)).to_have_text("Review inbox")
    expect(page.locator("#inbox-list")).to_contain_text("No enquiries yet. Add keywords and connect LinkedIn in Setup")
    expect(page.locator("header")).to_contain_text("FIXTURE MODE")
    expect(page.locator("#status-light")).to_contain_text("LinkedIn not connected")

    # --- error path: Run now is refused before LinkedIn is connected
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_disabled()
    expect(page.locator("#run-panel")).to_contain_text("Connect LinkedIn first")
    expect(page.locator("#run-history")).to_contain_text("No runs yet.")

    # --- setup: 4 default offerings, add the three corpus keywords (in this order)
    page.goto(f"{base}/setup")
    expect(page.locator("#offering-list")).to_contain_text("AI consulting & build services")
    expect(page.locator("#keyword-list")).to_contain_text("No keywords yet")
    for kw in RUN1_KEYWORDS:
        add_keyword(page, kw)
    expect(page.locator("#keyword-list li")).to_have_count(3)

    # --- Phase 1.1 card copy before Connect: the browser line + the sales0-profile explanation
    card_el = page.locator("#linkedin-card")
    expect(page.locator("#browser-note")).to_have_text("Browser: Built-in browser (Chromium)")
    expect(card_el).to_contain_text("sales0 opens Google Chrome with its own sales0 profile on this computer.")
    expect(card_el).to_contain_text("your everyday Chrome profile isn't touched")
    assert not [p for p in _request_paths(server) if p.startswith("/__fixtures/")], "no LinkedIn page before Connect"

    # --- Connect LinkedIn (fixture: login → 2FA → remember-browser → feed) → header light green
    connect_linkedin(page, base)
    expect(page.locator("#status-light")).to_contain_text("no runs yet")
    expect(card_el).to_contain_text("LinkedIn is connected. Checks and runs reuse this login invisibly")
    expect(page.locator("#connect-linkedin")).to_have_text("Connect again")
    _assert_connect_walked_the_2fa_flow(server)
    session = server.get_json("/api/session")
    assert session["status"] == "connected" and session["cooldown_until"] is None, session
    assert session["browser_label"] == "Built-in browser (Chromium)" and session["browser_note"] is None, session

    # --- Re-check: headless, green again, no new login
    logins_before = _request_paths(server).count(LOGIN_FLOW_PATHS[0])
    check = recheck_linkedin(page, server)
    assert check["result"] == "connected" and check["headless"] is True, check
    assert check["browser"] == "chromium" and check["cookie_present"] is True, check
    assert _request_paths(server).count(LOGIN_FLOW_PATHS[0]) == logins_before, "Re-check must not log in again"

    # --- Run now with live progress
    run_id, progress = run_now(page, base)
    assert progress, "the run finished before any live progress was rendered"
    assert progress["title"] == "Run in progress"
    assert progress["has_progress_bar"]
    assert all(word in progress["counters"] for word in ("Seen", "New", "Duplicates", "Triaged", "Elapsed"))

    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-status", "succeeded")
    expect(panel).to_contain_text("Run finished — 7 new enquiries")
    expect(panel.get_by_role("link", name="Open inbox (7 new)")).to_be_visible()

    detail = server.get_json(f"/api/runs/{run_id}")
    run = detail["run"]
    assert (run["status"], run["posts_seen"], run["posts_new"], run["posts_dupe"], run["enquiries_triaged"]) == (
        "succeeded", 9, 7, 2, 7,
    ), run
    assert run["triage_failed"] == 0 and run["cost_usd_micros"] > 0, run
    assert [(k["keyword_text"], k["posts_seen"]) for k in detail["keywords"]] == [
        ("corporate ai training", 3), ("ai workshop", 4), ("ai consulting", 2),
    ]

    # --- scrape_keyword log lines (Phase 1.2): SDUI parse + "Copy link to post" counts.
    # Fixture menus: corporate ai training = P1 noop, P2 urn, P3 none; ai workshop = P1 noop,
    # P5 urn, P6 noop, P7 none; ai consulting = P8 urn, P9 noop.
    lines = {e["keyword"]: e for e in server.log_events() if e.get("event") == "scrape_keyword"}
    assert {
        kw: (lines[kw]["strategy"], lines[kw]["permalink_attempts"], lines[kw]["permalinks_found"]) for kw in RUN1_KEYWORDS
    } == {
        "corporate ai training": ("sdui", 2, 1),
        "ai workshop": ("sdui", 3, 1),
        "ai consulting": ("sdui", 2, 1),
    }, lines

    history_row = page.locator("#run-history").get_by_text("Succeeded", exact=False).first
    expect(history_row).to_be_visible()
    page.goto(f"{base}/runs/{run_id}")
    keywords_table = page.locator("#run-keywords")
    for kw in RUN1_KEYWORDS:
        expect(keywords_table).to_contain_text(kw)

    # --- inbox: 7 cards with real Haiku content
    page.goto(f"{base}/inbox")
    expect(tab_count(page, "review")).to_have_text("7")
    expect(page.locator("#inbox-list article.card")).to_have_count(7)
    for pid in RUN1_NEW:
        c = card(page, pid)
        expect(c).to_have_count(1)
        expect(c).to_have_attribute("data-verdict", re.compile(r"^(genuine|discard)$"))
        expect(c.locator(".enquiry__summary")).not_to_have_text(re.compile(r"^\s*(No summary yet.*)?$"))
        expect(c.locator(".enquiry__cost")).to_have_text(COST_TEXT)
        assert "$0.00 " not in c.locator(".enquiry__cost").inner_text()
    expect(card(page, "P7")).to_have_count(0)  # content duplicate of P2, merged

    # --- Phase 1.2 card links (ui.md): no URN in the SDUI DOM, so a post link exists only when
    # "Copy link to post" yielded it (P5); P1's menu copies nothing → the author profile.
    p5_link = card(page, "P5").locator("a.enquiry__link")
    expect(p5_link).to_have_attribute("data-link-kind", "post")
    expect(p5_link).to_have_text(LINK_LABELS["post"])
    expect(p5_link).to_have_attribute("href", re.compile(rf"/feed/update/{re.escape(fixture_post('P5').urn)}/$"))
    expect(p5_link).to_have_attribute("target", "_blank")
    expect(p5_link).to_have_attribute("rel", "noopener")
    expect(card(page, "P5")).to_have_attribute("data-urn", fixture_post("P5").urn)
    p1_link = card(page, "P1").locator("a.enquiry__link")
    expect(p1_link).to_have_attribute("data-link-kind", "profile")
    expect(p1_link).to_have_text(LINK_LABELS["profile"])
    expect(p1_link).to_have_attribute("href", re.compile(r"/in/[^/?#]+/?$"))
    assert (card(page, "P1").get_attribute("data-urn") or "").startswith("ck:")

    p1 = card(page, "P1")
    expect(p1).to_have_attribute("data-verdict", "genuine")
    expect(p1.locator("a[href='mailto:training@acmelogistics.example']")).to_have_text("training@acmelogistics.example")
    expect(p1.locator(".enquiry__reason")).to_have_text(re.compile(r"\S{3,}"))
    expect(p1.locator(".chip")).to_have_text(["corporate ai training", "ai workshop"])
    expect(card(page, "P2").locator(".chip")).to_have_text(["corporate ai training", "ai workshop"])
    for pid in AI_DISCARDS:
        c = card(page, pid)
        expect(c).to_have_attribute("data-verdict", "discard")
        expect(c.locator(".badge--discard")).to_have_text("Discard")
        expect(c.locator(".enquiry__reason")).to_have_text(re.compile(r"\S{3,}"))
    expect(page.locator("#cost-badge")).to_contain_text(re.compile(r"AI today \$0\.\d{4}"))
    assert "AI today $0.00 " not in page.locator("#cost-badge").inner_text()

    # --- accept P1 → Accepted as "Prospect · New"
    p1.get_by_role("button", name="Accept").click()
    expect(tab_count(page, "accepted")).to_have_text("1")
    expect(tab_count(page, "review")).to_have_text("6")
    expect(card(page, "P1")).to_have_count(0)
    settled(page)

    # --- bulk discard the three AI discards with a reason
    for pid in AI_DISCARDS:
        card(page, pid).locator("input.js-select").check()
    expect(page.locator("#bulk-bar")).to_contain_text("3 selected")
    page.select_option("#bulk-discard-reason", BULK_REASON)
    page.locator("#bulk-bar").get_by_role("button", name="Discard selected").click()
    expect(page.locator("#toasts")).to_contain_text("Discarded 3")
    expect(tab_count(page, "discarded")).to_have_text("3")
    expect(tab_count(page, "review")).to_have_text("3")
    settled(page)

    # --- Discarded tab: kept, reasons shown, searchable (hit, narrow hit, no hit)
    page.goto(f"{base}/inbox?tab=discarded")
    expect(page.locator("#inbox-list article.card")).to_have_count(3)
    for pid in AI_DISCARDS:
        expect(card(page, pid)).to_contain_text(f"Discard reason: {BULK_REASON}")
    page.fill("#inbox-search", "freelance")
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"tab=discarded.*q=freelance"))
    expect(page.locator("#inbox-list article.card")).to_have_count(3)
    page.fill("#inbox-search", "SkillBridge")
    page.keyboard.press("Enter")
    expect(page.locator("#inbox-list article.card")).to_have_count(1)
    expect(card(page, "P2")).to_have_count(1)
    page.fill("#inbox-search", "zzz-no-such-text")
    page.keyboard.press("Enter")
    expect(page.locator("#inbox-list")).to_contain_text("No enquiries match 'zzz-no-such-text'.")

    # --- restart the server: accepted/discarded state and the connection survive
    calls_before_restart = len(_llm_calls(server))
    assert calls_before_restart >= 7
    server.restart()
    page.goto(f"{base}/inbox?tab=accepted")
    expect(page.locator("#inbox-list article.card")).to_have_count(1)
    accepted = card(page, "P1")
    expect(accepted).to_have_attribute("data-review-status", "accepted")
    expect(accepted).to_contain_text("Prospect · New")
    expect(accepted.locator("a[href='mailto:training@acmelogistics.example']")).to_be_visible()
    expect(tab_count(page, "discarded")).to_have_text("3")
    expect(tab_count(page, "review")).to_have_text("3")
    expect(page.locator("#status-light")).to_have_attribute("data-status", "connected")

    # --- run 2 on the restarted server: never-run keyword first, only P10/P11 new, few-shot used
    page.goto(f"{base}/setup")
    add_keyword(page, "claude code training")
    run2_id, _ = run_now(page, base)
    run2 = server.get_json(f"/api/runs/{run2_id}")
    assert run2["keywords"][0]["keyword_text"] == "claude code training"
    r2 = run2["run"]
    assert (r2["status"], r2["posts_seen"], r2["posts_new"], r2["posts_dupe"], r2["enquiries_triaged"]) == (
        "succeeded", 11, 2, 9, 2,
    ), r2
    run2_calls = [c for c in _llm_calls(server)[calls_before_restart:] if c.get("feature") == "triage"]
    assert len(run2_calls) >= 2
    assert all(c.get("fewshot_count", 0) >= 1 for c in run2_calls), [c.get("fewshot_count") for c in run2_calls]
    page.goto(f"{base}/inbox")
    expect(tab_count(page, "review")).to_have_text("5")
    expect(card(page, "P10")).to_have_attribute("data-verdict", re.compile(r"^(genuine|discard)$"))

    # --- server JSON log: llm_call + http_request lines, never the key
    log_text = server.log_text()
    assert re.search(r'"event":\s*"llm_call"', log_text)
    assert re.search(r'"event":\s*"http_request"', log_text)
    ok_calls = [c for c in _llm_calls(server) if c.get("status") == "ok"]
    assert len(ok_calls) >= 9
    assert all(c.get("input_tokens", 0) > 0 and c.get("cost_usd", 0) > 0 for c in ok_calls)
    assert any(e.get("event") == "http_request" and e.get("path") == "/inbox/decide" for e in server.log_events())
    key = get_settings().anthropic_api_key
    if key is not None and key.get_secret_value() in log_text:
        pytest.fail("the Anthropic API key leaked into the server log")
    assert_server_logs_clean(server)  # never the li_at cookie value

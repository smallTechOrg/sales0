"""Phase-1 golden path, end to end (roadmap gate assertion 6), re-proven through the Phase 1.1
connect + detection + parsing paths (Phase 1.1 gate assertions 2 and 3).

Live ``python -m sales0`` (fixture LinkedIn, port 8766, temp SQLite) + headless Chromium +
the REAL Haiku key from ``.env``:

setup (keywords) → Connect LinkedIn (headless fixture window walks login → 2FA →
"remember this browser" → feed, and only then turns green) → Re-check (headless) → Run now
(live progress) → run 1 counters
→ inbox shows ONLY the 4 genuine cards (real Haiku content + cost; Phase 1.4) → accept P1 →
Skipped by AI tab (P2/P3/P9, searchable) → Rescue P3 → Override P9 → server restart: state
survives → run 2 on the restarted server learns from the decisions (few-shot) and files only
P10 (P11 is skipped) → server JSON log has llm_call + http_request lines.
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
RUN1_GENUINE = ["P1", "P5", "P6", "P8"]
AI_DISCARDS = ["P2", "P3", "P9"]  # Phase 1.4: skipped by the AI, not inbox cards
SNIPPET = {"P2": "SkillBridge", "P3": "individual freelance trainer", "P9": "certified Generative AI trainer"}
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


def _skipped(page: Page, pid: str):
    return page.locator("#skipped-list article.skipped").filter(has_text=SNIPPET[pid])


def test_phase1_journey_setup_connect_run_triage_decide_search_restart(real_llm_server: LiveServer, ui: Page) -> None:
    server = real_llm_server
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

    # --- setup: 4 default offerings, add the three corpus keywords (in this order); no startup seeding in tests
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
    assert all(word in progress["counters"] for word in ("Scanned", "New to us", "Already seen", "Relevant", "Skipped by AI", "Elapsed"))

    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-status", "succeeded")
    expect(panel).to_contain_text("Run finished — 4 new enquiries")
    expect(panel).to_contain_text("Scanned 9 posts · 4 relevant")
    expect(panel.get_by_role("link", name="Open inbox (4 new)")).to_be_visible()

    detail = server.get_json(f"/api/runs/{run_id}")
    run = detail["run"]
    assert (run["status"], run["posts_seen"], run["posts_new"], run["posts_dupe"], run["enquiries_triaged"]) == (
        "succeeded", 9, 7, 2, 7,
    ), run
    assert (run["posts_relevant"], run["posts_skipped_ai"]) == (4, 3), run
    assert run["triage_failed"] == 0 and run["cost_usd_micros"] > 0, run
    assert [(k["keyword_text"], k["posts_seen"]) for k in detail["keywords"]] == [
        ("corporate ai training", 3), ("ai workshop", 4), ("ai consulting", 2),
    ]

    # --- scrape_keyword log lines: SDUI parse; menus are opened only for relevant posts (after triage)
    lines = {e["keyword"]: e for e in server.log_events() if e.get("event") == "scrape_keyword"}
    assert {kw: lines[kw]["strategy"] for kw in RUN1_KEYWORDS} == dict.fromkeys(RUN1_KEYWORDS, "sdui"), lines
    assert sum(lines[kw]["permalinks_found"] for kw in RUN1_KEYWORDS) == run["permalinks_found"] == 2, lines  # P5, P8
    assert sum(lines[kw]["permalink_attempts"] for kw in RUN1_KEYWORDS) <= 6, lines  # never the 9 scanned posts
    assert all(lines[kw]["passes"] >= 1 for kw in RUN1_KEYWORDS), lines

    history_row = page.locator("#run-history").get_by_text("Succeeded", exact=False).first
    expect(history_row).to_be_visible()
    page.goto(f"{base}/runs/{run_id}")
    keywords_table = page.locator("#run-keywords")
    for kw in RUN1_KEYWORDS:
        expect(keywords_table).to_contain_text(kw)

    # --- inbox: ONLY the 4 genuine posts, with real Haiku content
    page.goto(f"{base}/inbox")
    expect(tab_count(page, "review")).to_have_text("4")
    expect(tab_count(page, "skipped")).to_have_text("3")
    expect(page.locator("#inbox-list article.card")).to_have_count(4)
    for pid in RUN1_GENUINE:
        c = card(page, pid)
        expect(c).to_have_count(1)
        expect(c).to_have_attribute("data-verdict", "genuine")
        expect(c.locator(".enquiry__summary")).not_to_have_text(re.compile(r"^\s*(No summary yet.*)?$"))
        expect(c.locator(".enquiry__cost")).to_have_text(COST_TEXT)
        assert "$0.00 " not in c.locator(".enquiry__cost").inner_text()
    for pid in (*AI_DISCARDS, "P7"):  # the AI's discards (and P2's content duplicate) are not cards
        expect(card(page, pid)).to_have_count(0)

    # --- card links (ui.md): P5's "Copy link to post" worked -> View post; P1's menu copies nothing -> profile
    p5_link = card(page, "P5").locator("a.enquiry__link")
    expect(p5_link).to_have_attribute("data-link-kind", "post")
    expect(p5_link).to_have_text(LINK_LABELS["post"])
    expect(p5_link).to_have_attribute("href", re.compile(rf"/feed/update/{re.escape(fixture_post('P5').urn)}/$"))
    expect(p5_link).to_have_attribute("target", "_blank")
    expect(p5_link).to_have_attribute("rel", "noopener")
    assert (card(page, "P5").get_attribute("data-urn") or "") in fixture_post("P5").possible_refs()  # ck: row first, URN row added
    p1_link = card(page, "P1").locator("a.enquiry__link")
    expect(p1_link).to_have_attribute("data-link-kind", "profile")
    expect(p1_link).to_have_text(LINK_LABELS["profile"])
    expect(p1_link).to_have_attribute("href", re.compile(r"/in/[^/?#]+/?$"))
    expect(card(page, "P1")).to_contain_text("posted ~")
    assert (card(page, "P1").get_attribute("data-urn") or "").startswith("ck:")

    p1 = card(page, "P1")
    expect(p1.locator("a[href='mailto:training@acmelogistics.example']")).to_have_text("training@acmelogistics.example")
    expect(p1.locator(".enquiry__reason")).to_have_text(re.compile(r"\S{3,}"))
    expect(p1.locator(".chip")).to_have_text(["corporate ai training", "ai workshop"])
    expect(page.locator("#cost-badge")).to_contain_text(re.compile(r"AI today \$0\.\d{4}"))
    assert "AI today $0.00 " not in page.locator("#cost-badge").inner_text()

    # --- accept P1 -> Accepted as "Prospect · New"
    p1.get_by_role("button", name="Accept").click()
    expect(tab_count(page, "accepted")).to_have_text("1")
    expect(tab_count(page, "review")).to_have_text("3")
    expect(card(page, "P1")).to_have_count(0)
    settled(page)

    # --- Skipped by AI: P2/P3/P9 with the AI's reason; searchable (hit, narrow hit, no hit)
    page.goto(f"{base}/inbox?tab=skipped")
    expect(page.locator("#skipped-list article.skipped")).to_have_count(3)
    for pid in AI_DISCARDS:
        expect(_skipped(page, pid)).to_have_count(1)
        expect(_skipped(page, pid).locator(".skipped__reason")).to_have_text(re.compile(r"Skipped\s+\S{3,}"))
    page.fill("#inbox-search", "freelance")
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"tab=skipped.*q=freelance"))
    expect(_skipped(page, "P2")).to_have_count(1)
    expect(_skipped(page, "P3")).to_have_count(1)
    page.fill("#inbox-search", "SkillBridge")
    page.keyboard.press("Enter")
    expect(page.locator("#skipped-list article.skipped")).to_have_count(1)
    expect(_skipped(page, "P2")).to_have_count(1)
    page.fill("#inbox-search", "zzz-no-such-text")
    page.keyboard.press("Enter")
    expect(page.locator("#inbox-list")).to_contain_text("No skipped posts match 'zzz-no-such-text'.")

    # --- Rescue P3 -> To review (the AI's verdict stays Discard); Override P9 -> Accepted, "Overrode AI"
    page.goto(f"{base}/inbox?tab=skipped")
    _skipped(page, "P3").get_by_role("button", name="Rescue → enquiry").click()
    expect(page.locator("#skipped-list article.skipped")).to_have_count(2)
    settled(page)
    expect(tab_count(page, "review")).to_have_text("4")
    expect(tab_count(page, "skipped")).to_have_text("2")
    _skipped(page, "P9").get_by_role("button", name="Override: accept").click()
    expect(page.locator("#skipped-list article.skipped")).to_have_count(1)
    settled(page)
    expect(tab_count(page, "accepted")).to_have_text("2")
    expect(tab_count(page, "skipped")).to_have_text("1")
    page.goto(f"{base}/inbox")
    rescued = page.locator("#inbox-list article.card").filter(has_text="individual freelance trainer")
    expect(rescued).to_have_count(1)
    expect(rescued).to_have_attribute("data-verdict", "discard")
    expect(rescued.locator(".badge--discard")).to_have_text("Discard")
    page.goto(f"{base}/inbox?tab=accepted")
    overridden = page.locator("#inbox-list article.card").filter(has_text="certified Generative AI trainer")
    expect(overridden).to_contain_text("Overrode AI")
    expect(overridden).to_contain_text("Prospect · New")

    # --- restart the server: decisions, rescues and the connection survive
    calls_before_restart = len(_llm_calls(server))
    assert calls_before_restart >= 7
    server.restart()
    page.goto(f"{base}/inbox?tab=accepted")
    expect(page.locator("#inbox-list article.card")).to_have_count(2)
    accepted = card(page, "P1")
    expect(accepted).to_have_attribute("data-review-status", "accepted")
    expect(accepted).to_contain_text("Prospect · New")
    expect(accepted.locator("a[href='mailto:training@acmelogistics.example']")).to_be_visible()
    expect(tab_count(page, "review")).to_have_text("4")
    expect(tab_count(page, "skipped")).to_have_text("1")
    expect(page.locator("#status-light")).to_have_attribute("data-status", "connected")

    # --- run 2 on the restarted server: never-run keyword first; only P10/P11 are new; few-shot used
    page.goto(f"{base}/setup")
    add_keyword(page, "claude code training")
    run2_id, _ = run_now(page, base)
    run2 = server.get_json(f"/api/runs/{run2_id}")
    assert run2["keywords"][0]["keyword_text"] == "claude code training"
    r2 = run2["run"]
    assert (r2["status"], r2["posts_seen"], r2["posts_new"], r2["posts_dupe"], r2["enquiries_triaged"]) == (
        "succeeded", 11, 2, 9, 2,
    ), r2
    assert (r2["posts_relevant"], r2["posts_skipped_ai"]) == (1, 1), r2
    run2_calls = [c for c in _llm_calls(server)[calls_before_restart:] if c.get("feature") == "triage"]
    assert len(run2_calls) == 2, "only the 2 new posts are triaged; seen ones never again"
    assert all(c.get("fewshot_count", 0) >= 1 for c in run2_calls), [c.get("fewshot_count") for c in run2_calls]
    page.goto(f"{base}/inbox")
    expect(tab_count(page, "review")).to_have_text("5")
    expect(card(page, "P10")).to_have_attribute("data-verdict", "genuine")
    expect(card(page, "P11")).to_have_count(0)
    expect(tab_count(page, "skipped")).to_have_text("2")

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

"""Phase 1.4 golden path (roadmap gate assertion 10): the 60-phrase library -> one **Run now** that
returns at once and shows live counters -> a run that scans >= 200 posts -> an inbox of genuine
cards only (real Haiku verdicts, "View post" links) -> Skipped by AI (searchable, Rescue) -> a second
run that finds nothing new.

Live ``python -m sales0`` (fixture LinkedIn bulk corpus, port 8766, temp SQLite, 6 keywords per run)
+ headless Chromium + the REAL Haiku key from ``.env``."""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, expect

from tests.e2e._flows import connect_linkedin, run_now, settled, tab_count
from tests.support.live_server import LiveServer

BULK_RUN_TIMEOUT_MS = 540_000


def _counter(page: Page, name: str) -> int:
    text = page.locator(f'#run-panel [data-counter="{name}"]').first.inner_text()
    return int(re.sub(r"\D", "", text) or "0")


def test_suggested_keywords_then_a_deep_run_files_only_relevant_posts_and_run_two_finds_nothing_new(
    server: LiveServer, ui: Page
) -> None:
    page, base = ui, server.base_url

    # --- Setup: Add suggested keywords (60, idempotent)
    page.goto(f"{base}/setup")
    expect(page.locator("#keyword-list")).to_contain_text("No keywords yet")
    page.locator("#add-suggested-keywords").click()
    expect(page.locator("#toasts")).to_contain_text("Added 60 · 0 already present")
    expect(page.locator("#keyword-list [data-keyword-id]")).to_have_count(60)
    settled(page)
    page.locator("#add-suggested-keywords").click()
    expect(page.locator("#toasts")).to_contain_text("Added 0 · 60 already present")
    expect(page.locator("#keyword-list [data-keyword-id]")).to_have_count(60)
    connect_linkedin(page, base)

    # --- Run now returns in < 2 s and the live counters move
    page.goto(f"{base}/runs")
    expect(page.locator("#run-now")).to_be_enabled()
    started = time.monotonic()
    page.locator("#run-now").click()
    panel = page.locator("#run-panel")
    expect(panel).to_have_attribute("data-run-id", re.compile(r"^\d+$"), timeout=2_000)
    assert time.monotonic() - started < 2.0, "Run now must return at once"
    run_id = int(panel.get_attribute("data-run-id") or "0")
    deadline = time.monotonic() + 120
    seen_moving = 0
    while time.monotonic() < deadline:
        if panel.get_attribute("data-status") not in ("queued", "running"):
            break
        seen_moving = _counter(page, "seen") if page.locator('#run-panel [data-counter="seen"]').count() else 0
        if seen_moving > 0:
            break
        page.wait_for_timeout(100)
    assert seen_moving > 0, "the live Scanned counter never moved while the run was active"
    expect(panel.locator(".run-keyword-index")).to_contain_text(re.compile(r"Keyword \d+/6"))
    page.wait_for_function(
        "(s) => { const p = document.querySelector('#run-panel'); return p && s.includes(p.dataset.status) && !p.hasAttribute('hx-get'); }",
        arg=["succeeded", "aborted", "failed"],
        timeout=BULK_RUN_TIMEOUT_MS,
    )
    expect(panel).to_have_attribute("data-status", "succeeded")

    run = server.get_json(f"/api/runs/{run_id}")["run"]
    assert run["status"] == "succeeded" and run["keywords_planned"] == 6 == run["keywords_done"], run
    assert run["posts_seen"] >= 200, run
    assert run["posts_new"] + run["posts_dupe"] == run["posts_seen"] and run["posts_relevant"] > 0, run
    assert run["posts_relevant"] + run["posts_skipped_ai"] + run["triage_failed"] == run["posts_new"], run
    assert run["posts_skipped_ai"] > 0 and run["permalinks_found"] > 0 and run["cost_usd_micros"] > 0, run
    expect(panel).to_contain_text(f"Scanned {run['posts_seen']} posts · {run['posts_relevant']} relevant")
    assert _counter(page, "seen") == run["posts_seen"] and _counter(page, "relevant") == run["posts_relevant"]

    # --- Inbox: only genuine cards (real Haiku verdicts), at least one with View post
    page.goto(f"{base}/inbox")
    relevant = run["posts_relevant"]
    expect(tab_count(page, "review")).to_have_text(str(relevant))
    expect(tab_count(page, "skipped")).to_have_text(str(run["posts_skipped_ai"]))
    shown = min(relevant, 25)  # one inbox page; the recall-first prompt files many more genuine posts
    expect(page.locator("#inbox-list article.card")).to_have_count(shown)
    expect(page.locator("#inbox-list article.card[data-verdict='genuine']")).to_have_count(shown)
    expect(page.locator("#inbox-list a.enquiry__link[data-link-kind='post']").first).to_have_text("View post ↗")
    assert page.locator("#inbox-list a.enquiry__link[data-link-kind='post']").count() >= 1

    # --- Skipped by AI: non-empty, searchable, Rescue one
    page.goto(f"{base}/inbox?tab=skipped")
    skipped = page.locator("#skipped-list article.skipped")
    expect(skipped.first).to_be_visible()
    first_snippet = skipped.first.locator(".skipped__snippet").inner_text()
    needle = " ".join(first_snippet.split()[:4])
    page.fill("#inbox-search", needle)
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"tab=skipped.*q="))
    expect(skipped.first).to_contain_text(needle)
    page.fill("#inbox-search", "zzz-no-such-text")
    page.keyboard.press("Enter")
    expect(page.locator("#inbox-list")).to_contain_text("No skipped posts match 'zzz-no-such-text'.")
    page.goto(f"{base}/inbox?tab=skipped")
    page.locator("#skipped-list article.skipped").first.get_by_role("button", name="Rescue → enquiry").click()
    settled(page)
    expect(tab_count(page, "review")).to_have_text(str(relevant + 1))
    expect(tab_count(page, "skipped")).to_have_text(str(run["posts_skipped_ai"] - 1))

    # --- A run plans never-run keywords first, so pause the 54 not yet run: run 2 re-searches the same six
    page.goto(f"{base}/setup")
    unrun = page.locator("#keyword-list li[data-active='true']").filter(has_text="not run yet")
    ids = [li.get_attribute("data-keyword-id") for li in unrun.all()]
    assert len(ids) == 54
    for kid in ids:
        assert page.request.post(f"{base}/setup/keywords/{kid}/toggle", form={"active": "false"}).ok

    # --- Run 2: the same posts are all "already seen"; nothing new, nothing relevant, no new AI calls
    llm_calls_before = len([e for e in server.log_events() if e.get("event") == "llm_call" and e.get("feature") == "triage"])
    run2_id, _ = run_now(page, base, timeout_ms=BULK_RUN_TIMEOUT_MS)
    run2 = server.get_json(f"/api/runs/{run2_id}")["run"]
    assert run2["status"] == "succeeded" and run2["posts_seen"] >= 200, run2
    assert (run2["posts_new"], run2["posts_relevant"], run2["posts_skipped_ai"]) == (0, 0, 0), run2
    assert run2["posts_dupe"] == run2["posts_seen"], run2
    expect(page.locator("#run-panel")).to_contain_text("Scanned")
    assert _counter(page, "new") == 0 and _counter(page, "relevant") == 0
    llm_calls_after = len([e for e in server.log_events() if e.get("event") == "llm_call" and e.get("feature") == "triage"])
    assert llm_calls_after == llm_calls_before, "a post already seen is never triaged again"
    page.goto(f"{base}/inbox")
    expect(tab_count(page, "review")).to_have_text(str(relevant + 1))

    # --- Setup shows each keyword's yield
    page.goto(f"{base}/setup")
    expect(page.locator("#keyword-list .keyword__yield").filter(has_text=re.compile(r"Scanned \d+ · Relevant \d+")).first).to_be_visible()

"""Prospect watch end to end: real server subprocess (fixture LinkedIn), headless Chromium, REAL Haiku.

Discovery finds the corpus posts; the user tracks an author from an inbox card; two more fake
profiles are tracked; Run watch now shows live progress and a per-profile log; the second pass finds
nothing new (and the page says so); untrack/pause work; the scheduler lists the job disabled."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, expect
from sqlalchemy import create_engine, text

from tests.e2e._flows import RUN_TIMEOUT_MS, TERMINAL_RUN_STATUSES, add_keyword, card, connect_linkedin, run_now, settled
from tests.e2e.conftest import _free_port
from tests.e2e._fast import live_server
from tests.support.live_server import LiveServer


def seed_tracked(server: LiveServer, slug: str, name: str) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    engine = create_engine(server.db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO linkedin_profiles (slug, kind, profile_url, display_name, headline, first_seen_at, last_seen_at,"
                " posts_seen, relevant_count, tracked, watch_checks, watch_hits) VALUES (:s, :k, :u, :n, 'Fixture', :t, :t, 0, 0, 1, 0, 0)"
            ),
            {"s": slug, "k": "company" if slug.startswith("company/") else "person", "u": f"https://www.linkedin.com/{slug}/", "n": name, "t": now},
        )
    engine.dispose()


def make_due(server: LiveServer) -> None:
    engine = create_engine(server.db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE linkedin_profiles SET last_checked_at = NULL"))
    engine.dispose()


def run_watch(page: Page, base: str) -> int:
    page.goto(f"{base}/tracked")
    button = page.locator("#watch-run-now")
    expect(button).to_be_enabled()
    panel = page.locator("#watch-panel")
    previous = panel.get_attribute("data-run-id") or ""  # the last run's panel is shown until the click swaps it
    button.click()
    expect(panel).to_have_attribute("data-run-id", re.compile(r"^\d+$"))
    page.wait_for_function("(prev) => document.querySelector('#watch-panel').dataset.runId !== prev", arg=previous)
    run_id = int(panel.get_attribute("data-run-id") or "0")
    page.wait_for_function(
        "(statuses) => { const p = document.querySelector('#watch-panel');"
        " return p && statuses.includes(p.dataset.status) && !p.hasAttribute('hx-get'); }",
        arg=list(TERMINAL_RUN_STATUSES),
        timeout=RUN_TIMEOUT_MS,
    )
    return run_id


def test_track_run_watch_dedupe_pause_untrack_and_schedule(tmp_path: Path, ui: Page) -> None:
    with live_server(tmp_path / "watch", port=_free_port(8796), extra_env={
        "SALES0_WATCH_PROFILE_PAUSE_MIN_S": "0.05", "SALES0_WATCH_PROFILE_PAUSE_MAX_S": "0.1",
    }) as server:
        page, base = ui, server.base_url
        connect_linkedin(page, base)
        page.goto(f"{base}/setup")
        add_keyword(page, "corporate ai training")
        run_now(page, base)

        # Track the author of the first corpus post straight from its inbox card.
        page.goto(f"{base}/inbox")
        p1 = card(page, "P1")
        expect(p1).to_have_count(1)
        p1.locator('[data-action="track"]').click()
        expect(p1.locator('[data-tracked="1"]')).to_be_visible()
        settled(page)
        seed_tracked(server, "in/watch-asha-rao", "Asha Rao")
        seed_tracked(server, "company/watch-brightfield", "Brightfield Pharma")

        page.goto(f"{base}/tracked")
        expect(page.locator("#tracked-table tbody tr")).to_have_count(3)
        expect(page.locator("#watch-panel")).to_contain_text("3 profiles due now")

        # Run watch now: live progress, then the per-profile log.
        run_id = run_watch(page, base)
        panel = page.locator("#watch-panel")
        expect(panel).to_have_attribute("data-status", "succeeded")
        log = page.locator("#watch-log")
        expect(log.locator("tr[data-slug]")).to_have_count(3)
        asha = log.locator('tr[data-slug="in/watch-asha-rao"]')
        expect(asha).to_contain_text("2")  # two posts seen on her feed
        assert int(asha.locator("td").nth(4).inner_text()) == 1  # relevant: the requirement, not the safety-award post
        priya = log.locator('tr[data-slug="in/priya-menon-acme"]')
        assert int(priya.locator("td").nth(3).inner_text()) == 0  # her feed only has what discovery already saw (new = 0)
        expect(page.locator(".watch-summary")).to_contain_text("hits 2")

        # The new enquiries are in the inbox, tagged as found by the watch.
        page.goto(f"{base}/inbox")
        expect(page.locator("#inbox-list").get_by_text("watch:in/watch-asha-rao")).to_have_count(1)
        expect(page.locator("#inbox-list").get_by_text("watch:company/watch-brightfield")).to_have_count(1)

        # Tracked list: last checked + hits; nothing due now (3 day spacing) -> Run is disabled with the reason.
        page.goto(f"{base}/tracked")
        expect(page.locator('#tracked-table tr[data-slug="in/watch-asha-rao"]')).to_contain_text("ok")
        expect(page.locator("#watch-run-now")).to_be_disabled()
        expect(page.locator("#watch-run-reason")).to_have_attribute("data-reason-code", "nothing_due")

        # Second pass (made due again): idempotent - 0 new, 0 relevant.
        make_due(server)
        run_watch(page, base)
        second = page.locator("#watch-log tr[data-slug]")
        expect(second).to_have_count(3)
        for i in range(3):
            assert int(second.nth(i).locator("td").nth(3).inner_text()) == 0
            assert int(second.nth(i).locator("td").nth(4).inner_text()) == 0

        # Pause one profile -> due drops; resume; untrack another.
        page.goto(f"{base}/tracked")
        page.locator('#tracked-table tr[data-slug="company/watch-brightfield"]').get_by_role("button", name="Pause").click()
        expect(page.locator('#tracked-table tr[data-slug="company/watch-brightfield"]')).to_have_attribute("data-paused", "1")
        page.locator('#tracked-table tr[data-slug="company/watch-brightfield"]').get_by_role("button", name="Resume").click()
        expect(page.locator('#tracked-table tr[data-slug="company/watch-brightfield"]')).to_have_attribute("data-paused", "0")
        page.locator('#tracked-table tr[data-slug="in/watch-asha-rao"]').get_by_role("button", name="Untrack").click()
        expect(page.locator("#tracked-table tbody tr")).to_have_count(2)

        # The run also shows on the Runs page (shared run history); the scheduler lists the job, disabled by default.
        page.goto(f"{base}/runs/{run_id}")
        expect(page.locator("main")).to_contain_text("watch:in/watch-asha-rao")
        page.goto(f"{base}/schedule")
        job = page.locator("#job-profile_watch")
        expect(job).to_have_attribute("data-enabled", "0")
        job.get_by_role("button", name="Enable").click()
        expect(page.locator("#job-profile_watch")).to_have_attribute("data-enabled", "1")
        expect(page.locator("#job-profile_watch .next-run")).not_to_contain_text("Disabled")

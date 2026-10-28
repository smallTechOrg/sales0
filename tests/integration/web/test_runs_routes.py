"""Runs page (slice G): Run now eligibility, live progress polling, outcomes, history and
run detail. The journey test drives a real run end to end: real uvicorn server in fixture
mode, real headless Chromium against the fake LinkedIn, real Haiku triage, real SQLite."""

from __future__ import annotations

import re
import time
from datetime import timedelta
from html import unescape

import httpx
import pytest
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.linkedin_fixture.corpus import post_by_id
from tests.integration.web.conftest import HX, make_run, set_session


def _panel(html: str) -> str:
    start = html.index('<section id="run-panel"')
    return html[start : html.index("</section>", start)]


def _run_now_disabled(html: str) -> bool:
    tag = re.search(r'<button id="run-now"[^>]*>', html)
    assert tag, "no Run now button"
    return re.search(r"\sdisabled[\s>]", tag.group(0)) is not None


def _is_toast_error(resp: httpx.Response, text: str) -> bool:
    return resp.headers.get("HX-Retarget") == "#toasts" and "toast--error" in resp.text and text in unescape(resp.text)


def test_empty_runs_page_disables_run_now_until_linkedin_is_connected(client: TestClient) -> None:
    html = unescape(client.get("/runs").text)
    panel = _panel(html)
    assert _run_now_disabled(panel)
    assert "Connect LinkedIn first (Setup → Connect LinkedIn)." in panel
    assert "Today: 0/50 runs · 0/2000 posts scanned · max 40 keywords × 80 posts per run" in panel
    assert "No runs yet." in html.split('id="run-history"', 1)[1]

    refused = client.post("/runs", headers=HX)
    assert _is_toast_error(refused, "Connect LinkedIn first")  # type: ignore[arg-type]


def test_connected_without_keywords_explains_and_refuses(client: TestClient) -> None:
    set_session("connected")
    panel = unescape(_panel(client.get("/runs").text))
    assert "Add at least one active keyword in Setup." in panel and _run_now_disabled(panel)
    assert _is_toast_error(client.post("/runs", headers=HX), "Add at least one active keyword in Setup.")  # type: ignore[arg-type]


def test_daily_run_cap_disables_run_now_with_the_count(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.config import reset_settings

    monkeypatch.setenv("SALES0_SCRAPE_MAX_RUNS_PER_DAY", "1")
    reset_settings()
    set_session("connected")
    client.post("/setup/keywords", data={"text": "ai workshop"}, headers=HX)
    make_run("succeeded", posts_seen=4)
    panel = unescape(_panel(client.get("/runs").text))
    assert "Daily run limit reached (1/1)." in panel and _run_now_disabled(panel)
    assert "Today: 1/1 runs · 4/2000 posts scanned" in panel


def test_running_run_polls_every_second_with_counters(client: TestClient) -> None:
    run_id = make_run(
        "running", keywords_planned=3, keywords_done=1, posts_seen=4, posts_new=3, posts_dupe=1, enquiries_triaged=2,
        current_step="Searching 'ai workshop' (2/3) — scrolling", current_keyword="ai workshop",
        posts_relevant=1, posts_skipped_ai=1, permalinks_found=1,
    )
    html = unescape(client.get("/runs").text)
    panel = _panel(html)
    assert f'hx-get="/runs/{run_id}/progress" hx-trigger="every 1s"' in panel
    assert re.search(r'<progress id="run-progress" class="progress" max="3" value="1">', panel)
    assert "Searching 'ai workshop' (2/3) — scrolling" in panel
    text = re.sub(r"<[^>]+>", "", panel)
    text = re.sub(r"\s+", " ", text)
    assert "Scanned 4 New to us 3 Already seen 1 Relevant 1 Skipped by AI 1 Post links 1" in text
    assert "Keyword 2/3 · ai workshop" in text and "You can close this tab" in text
    assert 'id="run-summary"' in panel and 'data-scanned="4"' in panel and 'data-new="3"' in panel
    assert 'data-relevant="1"' in panel and 'data-skipped="1"' in panel
    assert "Scanned 4 posts · 1 relevant (33% of the 3 new ones)" in text
    assert "status-light" in html

    progress = client.get(f"/runs/{run_id}/progress", headers=HX)
    assert 'hx-trigger="every 1s"' in progress.text and "HX-Trigger" not in progress.headers
    assert 'id="cost-badge" class="cost-badge" hx-swap-oob="true"' in progress.text


def test_finished_progress_stops_polling_and_shows_outcome(client: TestClient) -> None:
    ok_id = make_run("succeeded", keywords_planned=3, keywords_done=3, posts_seen=9, posts_new=7, posts_dupe=2, enquiries_triaged=7,
                      posts_relevant=3, posts_skipped_ai=4)
    done = client.get(f"/runs/{ok_id}/progress", headers=HX)
    assert "every 1s" not in done.text.split('id="run-history"')[0]
    assert done.headers["HX-Trigger"] == "session-changed"
    assert "Run finished — 3 new enquiries" in done.text and "Open inbox (3 new)" in done.text
    assert 'id="run-summary"' in done.text and "Scanned 9 posts · 3 relevant (43% of the 7 new ones)" in unescape(done.text)
    assert 'id="run-history" hx-swap-oob="true"' in done.text

    aborted = make_run(
        "aborted", abort_reason="checkpoint", keywords_planned=3, keywords_done=1,
        keywords=[("checkpoint-test", "error"), ("ai workshop", "skipped"), ("ai consulting", "skipped")],
    )
    body = unescape(client.get(f"/runs/{aborted}/progress", headers=HX).text)
    assert "Run stopped: LinkedIn checkpoint detected" in body and 'role="alert"' in body
    assert "Connect LinkedIn in Setup" in body

    failed = make_run("failed", error_message="Interrupted by server restart")
    assert "Interrupted by server restart" in client.get(f"/runs/{failed}/progress", headers=HX).text


def test_not_logged_in_failure_links_to_setup_and_stopped_run_guides_to_connect(client: TestClient) -> None:
    message = "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup"
    failed = make_run("failed", error_message=message, keywords=[("ai workshop", "skipped")])
    body = unescape(client.get(f"/runs/{failed}/progress", headers=HX).text)
    panel = body.split('id="run-history"')[0]
    assert message in panel and 'href="/setup#linkedin-card"' in panel and "Go to Setup" in panel
    assert "Go to Setup" in unescape(client.get(f"/runs/{failed}").text)

    other = make_run("failed", error_message="Interrupted by server restart")
    assert "Go to Setup" not in client.get(f"/runs/{other}/progress", headers=HX).text.split('id="run-history"')[0]

    stopped = make_run("aborted", abort_reason="logout", keywords=[("logout-test", "error")])
    body = unescape(client.get(f"/runs/{stopped}/progress", headers=HX).text)
    assert "Press Connect LinkedIn in Setup — the sales0 Chrome window opens on LinkedIn so you can resolve it." in body
    assert "normal browser" not in body


def test_history_and_detail_show_per_keyword_log(client: TestClient) -> None:
    started = utcnow() - timedelta(minutes=3)
    run_id = make_run(
        "aborted", abort_reason="checkpoint", started_at=started, finished_at=started + timedelta(seconds=75),
        keywords_planned=2, keywords_done=1, posts_seen=0,
        keywords=[("checkpoint-test", "error"), ("ai workshop", "skipped")],
    )
    history = client.get("/runs").text.split('id="run-history"', 1)[1]
    assert f'href="/runs/{run_id}"' in history and "Aborted" in history and "1m 15s" in history

    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    table = detail.text.split('id="run-keywords"', 1)[1].split("</table>", 1)[0]
    assert table.index("checkpoint-test") < table.index("ai workshop")
    assert "Skipped" in table and "Error" in table
    assert "Run stopped: LinkedIn checkpoint detected" in detail.text

    api = client.get(f"/api/runs/{run_id}").json()
    assert api["ok"] is True and api["data"]["run"]["abort_reason"] == "checkpoint"
    assert [k["status"] for k in api["data"]["keywords"]] == ["error", "skipped"]


def test_unknown_run_is_a_human_404_page_and_json_error(client: TestClient) -> None:
    page = client.get("/runs/9999")
    assert page.status_code == 404 and "That item no longer exists" in page.text and "Traceback" not in page.text
    api = client.get("/api/runs/9999")
    assert api.status_code == 404 and api.json() == {
        "ok": False,
        "error": {"code": "not_found", "message": "That item no longer exists — refresh the page."},
    }


# --------------------------------------------------------------------------- real journey
def _ref_of(cards: dict[str, str], post_id: str) -> str:
    """The one rendered ``data-urn`` among the corpus post's ``possible_refs()``."""
    post = post_by_id(post_id)
    assert post is not None, post_id
    hits = [ref for ref in post.possible_refs() if ref in cards]
    assert len(hits) == 1, (post_id, post.possible_refs(), sorted(cards))
    return hits[0]


@pytest.mark.llm
@pytest.mark.browser
@pytest.mark.timeout(240)
def test_connect_then_run_now_shows_live_progress_and_triaged_cards(fixture_server: str) -> None:
    """Setup → Connect (fixture LinkedIn, headless Chromium) → Run now → poll progress →
    outcome → Inbox shows real Haiku verdicts for the 'ai consulting' corpus posts (P8, P9)."""
    with httpx.Client(base_url=fixture_server, timeout=30) as http:
        added = http.post("/setup/keywords", data={"text": "ai consulting"}, headers=HX)
        assert "<strong>ai consulting</strong>" in added.text

        card = http.post("/setup/linkedin/connect", headers=HX)
        assert card.headers.get("HX-Trigger") == "session-changed"
        deadline = time.monotonic() + 60
        status = ""
        while time.monotonic() < deadline:
            card = http.get("/setup/linkedin/status", params={"prev": "busy:connect"}, headers=HX)
            status = re.search(r'data-status="([a-z_]+)"', card.text).group(1)  # type: ignore[union-attr]
            if status != "busy":
                break
            time.sleep(0.5)
        assert status == "connected", card.text
        assert "LinkedIn connected" in http.get("/_shell/status-light", headers=HX).text

        assert not _run_now_disabled(http.get("/runs").text)
        started = http.post("/runs", headers=HX)
        assert started.headers.get("HX-Trigger") == "session-changed"
        run_id = int(re.search(r'data-run-id="(\d+)"', started.text).group(1))  # type: ignore[union-attr]
        assert f'hx-get="/runs/{run_id}/progress" hx-trigger="every 1s"' in started.text

        seen_steps: set[str] = set()
        deadline = time.monotonic() + 180
        final = ""
        while time.monotonic() < deadline:
            progress = http.get(f"/runs/{run_id}/progress", headers=HX).text
            panel = progress.split('id="run-history"')[0]
            step = re.search(r'class="run-step">([^<]*)<', panel)
            if step:
                seen_steps.add(unescape(step.group(1)))
            if 'hx-trigger="every 1s"' not in panel:
                final = unescape(progress)
                break
            time.sleep(0.5)
        assert final, f"run did not finish; steps seen: {seen_steps}"
        # Phase 1.4: only the AI-genuine post (P8) becomes an enquiry; P9 is "Skipped by AI".
        assert "Run finished — 1 new enquiry" in final and "Open inbox (1 new)" in final
        assert seen_steps, "no live progress was rendered while the run was active"

        data = http.get(f"/api/runs/{run_id}").json()["data"]
        assert data["run"]["status"] == "succeeded"
        run = data["run"]
        assert (run["posts_seen"], run["posts_new"], run["posts_relevant"], run["posts_skipped_ai"]) == (2, 2, 1, 1)
        assert run["cost_usd_micros"] > 0
        assert 'id="run-summary"' in final and 'data-scanned="2"' in final and 'data-relevant="1"' in final

        detail = http.get(f"/runs/{run_id}").text
        assert "ai consulting" in detail.split('id="run-keywords"', 1)[1] and "Done" in detail
        assert 'id="run-summary"' in detail

        inbox = unescape(http.get("/inbox").text)
        assert "To review (<span data-count=\"review\">1</span>)" in inbox
        assert "Skipped by AI (<span data-count=\"skipped\">1</span>)" in inbox
        cards = {
            m.group(1): m.group(0)
            for m in re.finditer(r'<article id="enquiry-\d+"[^>]*data-urn="([^"]+)".*?</article>', inbox, re.S)
        }
        p8_ref = _ref_of(cards, "P8")
        # data-urn is the FIRST source ref (a ck: key); the permalink captured after triage only sets the link.
        p8 = cards[p8_ref]
        assert 'data-verdict="genuine"' in p8 and 'href="mailto:it-projects@meridian.example"' in p8
        assert re.search(r"AI \$0\.\d{4} · [\d.]+k? tok", p8)
        assert "View post ↗" in p8 and 'data-link-kind="post"' in p8

        skipped = unescape(http.get("/inbox", params={"tab": "skipped"}).text)
        assert 'id="skipped-list"' in skipped and skipped.count('class="skipped"') == 1
        assert 'data-verdict="discard"' in skipped and "skipped__rescue" in skipped
        badge = re.search(r"AI today <strong>\$(\d+\.\d+)</strong>", inbox)
        assert badge and float(badge.group(1)) > 0


# --------------------------------------------------------------------------- audit follow-ups
def _history_row(html: str, run_id: int) -> str:
    start = html.index(f'<tr id="run-row-{run_id}">')
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[start : html.index("</tr>", start)])).strip()


def test_active_progress_poll_refreshes_the_history_row_out_of_band(client: TestClient) -> None:
    run_id = make_run("queued", keywords_planned=0, keywords_done=0)
    first = client.get(f"/runs/{run_id}/progress", headers=HX).text
    assert 'id="run-history" hx-swap-oob="true"' in first
    assert "Queued 0/0" in _history_row(first, run_id)

    from sales0.db.engine import session_scope
    from sales0.db.models import ScrapeRun

    with session_scope() as db:
        run = db.get(ScrapeRun, run_id)
        assert run is not None
        run.status, run.keywords_planned, run.keywords_done, run.posts_seen, run.posts_new = "running", 3, 2, 6, 5
    poll = client.get(f"/runs/{run_id}/progress", headers=HX)
    assert 'hx-trigger="every 1s"' in poll.text.split('id="run-history"')[0]  # still polling
    assert "HX-Trigger" not in poll.headers  # header light only refreshes on the final render
    assert 'id="run-history" hx-swap-oob="true"' in poll.text
    row = _history_row(poll.text, run_id)
    assert "Running 2/3 6 5" in row, row


def test_fresh_runs_page_after_a_run_shows_the_last_outcome_without_tracking_it(client: TestClient) -> None:
    make_run("succeeded", posts_new=1)  # older run — only the latest is summarised
    ok_id = make_run("succeeded", keywords_planned=2, keywords_done=2, posts_seen=5, posts_new=4, posts_relevant=4, posts_skipped_ai=0)
    panel = unescape(_panel(client.get("/runs").text))
    assert "Last run finished — 4 new enquiries" in panel and "Open inbox (4 new)" in panel
    assert f'data-last-run-id="{ok_id}"' in panel and f'href="/runs/{ok_id}"' in panel
    # The panel does not claim the old run as its own (clients wait for data-run-id of a NEW run).
    assert "data-run-id" not in panel.split(">", 1)[0] and "hx-get" not in panel.split(">", 1)[0]
    assert '<button id="run-now"' in panel

    aborted = make_run("aborted", abort_reason="captcha")
    panel = unescape(_panel(client.get("/runs").text))
    assert "Last run stopped: LinkedIn captcha detected" in panel and 'role="alert"' in panel
    assert "Connect LinkedIn in Setup" in panel and f'data-last-run-id="{aborted}"' in panel
    assert "Open inbox" not in panel

    make_run("failed", error_message="Interrupted by server restart")
    assert "Last run failed: Interrupted by server restart" in unescape(_panel(client.get("/runs").text))


def test_fresh_runs_page_with_no_runs_or_an_active_run_has_no_last_outcome(client: TestClient) -> None:
    assert "Last run" not in _panel(client.get("/runs").text)
    make_run("succeeded", posts_new=3)
    active = make_run("running", keywords_planned=2, keywords_done=0)
    panel = _panel(client.get("/runs").text)
    assert "Last run" not in panel and f'data-run-id="{active}"' in panel and "Run in progress" in panel


# ---------------------------------------------------------------------- Phase 1.4
def test_server_restart_interruption_is_an_amber_outcome_not_red(client: TestClient) -> None:
    text = "Interrupted by server restart — the next run continues with the keywords it had not reached"
    run_id = make_run("aborted", error_message=text, keywords_planned=40, keywords_done=12, posts_seen=210, posts_new=90,
                      posts_relevant=5, posts_skipped_ai=85, keywords=[("ai workshop", "done"), ("ai consulting", "skipped")])
    panel = unescape(client.get(f"/runs/{run_id}/progress", headers=HX).text).split('id="run-history"')[0]
    assert text in panel and "outcome--warn" in panel and "outcome--error" not in panel and 'role="alert"' not in panel
    assert "Run stopped" not in panel and "Next run allowed" not in panel
    assert 'data-scanned="210"' in panel and 'data-relevant="5"' in panel
    detail = unescape(client.get(f"/runs/{run_id}").text)
    assert text in detail and "outcome--warn" in detail


def test_history_and_detail_use_the_scanned_vs_relevant_columns(client: TestClient) -> None:
    run_id = make_run("succeeded", keywords_planned=1, keywords_done=1, posts_seen=210, posts_new=100, posts_dupe=110,
                      posts_relevant=6, posts_skipped_ai=94, permalinks_found=5,
                      keywords=[("ai workshop", "done")])
    page = unescape(client.get("/runs").text)
    head = page.split('id="run-history"', 1)[1].split("</thead>", 1)[0]
    cols = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', head)
    assert cols == ["Started", "Status", "Keywords", "Scanned", "New", "Relevant", "Skipped", "Cost", "Duration"]
    assert "1/1 210 100 6 94 $0.00" in _history_row(page, run_id)
    detail = unescape(client.get(f"/runs/{run_id}").text)
    kw_head = detail.split('id="run-keywords"', 1)[1].split("</thead>", 1)[0]
    kw_cols = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', kw_head)
    assert kw_cols == ["Keyword", "Status", "Scanned", "New", "Already seen", "Relevant", "Skipped", "Links", "Cost",
                       "Duration", "Message"]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", detail.split('id="run-summary"', 1)[1].split("</div>", 1)[0]))
    assert "Scanned 210 posts · 6 relevant (6.0% of the 100 new ones)" in text
    assert "Post links 5" in text and "Relevance 6.0% of new posts" in text and "AI cost $0.00" in text
    assert "Run now starts a long background run — you can close this tab; progress is kept." in unescape(client.get("/runs").text)

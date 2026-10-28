"""Every page route renders through the REAL app, ``render_page`` and ``shell_context`` in
every seeded LinkedIn/run state (architecture.md → Tests → Every-page render test).

The user once hit a 500 because a template used a shell/context key its route did not
provide. This test GETs every HTML page (and the shell/status fragments) once per state and
asserts HTTP 200 (404 for the error page) plus the page's key text and the header light's
label. The route list is checked against ``app.routes`` so a new page cannot skip it."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from html import unescape

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from sales0.clock import utcnow
from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.jobs.runner import get_runner, reset_runner
from sales0.services import inbox as inbox_service
from sales0.web.app import create_app
from tests.integration.web.conftest import make_enquiry, make_keyword, make_run, make_seen_post, set_session, triaged

# (path, key text) — full pages. "/" follows its redirect to the inbox.
STUB_PAGES = {
    "/pipeline": 2,
    "/contacts": 2,
    "/competitors": 2,
    "/import": 2,
    "/metrics": 4,
    "/whatsapp": 5,
}
INBOX_PAGES = ("/", "/inbox?tab=review", "/inbox?tab=accepted", "/inbox?tab=discarded", "/inbox?tab=discarded&q=zzz",
               "/inbox?tab=skipped", "/inbox?tab=skipped&state=failed", "/inbox?tab=skipped&q=zzz")
# Parameter-free GET routes that return HTML and are covered below (pages + fragments).
COVERED_PARAMLESS = {"/", "/inbox", "/setup", "/runs", *STUB_PAGES, "/_shell/status-light", "/_shell/banner", "/setup/linkedin/status", "/team", "/oauth/google/callback",
                    "/outreach", "/outreach/compose", "/outreach/compose/defaults", "/outreach/templates", "/outreach/decks"}

FALLBACK_NOTE = "Google Chrome not found — using the built-in browser"


@dataclass
class State:
    name: str
    light: str                                   # header status-light label (substring)
    run_id: int = 0                              # the seeded finished run for /runs/{id}
    progress_run_id: int = 0                     # the run whose /progress fragment is fetched
    runs_key: str = "Run now"
    setup_key: str = "Connect LinkedIn"
    banner: str | None = None                    # banner text when needs_attention
    extra_setup: list[str] = field(default_factory=list)
    extra_runs: list[str] = field(default_factory=list)
    extra_detail: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- seeding helpers
def _seed_inbox() -> None:
    """One card per tab, so every inbox tab renders real cards, not only empty states."""
    review = make_enquiry("P1", **triaged("genuine", "Logistics firm wants a GenAI workshop for 40 staff"))
    accepted = make_enquiry("P8", **triaged("genuine", "Insurer wants an AI assistant built", offering_id=3))
    discarded = make_enquiry("P2", **triaged("discard", "Training agency sourcing trainers", priority="low", offering_id=None))
    assert review
    # Phase 1.4: one skipped and one failed post so the Skipped tab renders real rows, and a grouped keyword.
    make_seen_post()
    make_seen_post("Another post that failed triage", verdict=None, reason=None, triage_status="failed",
                   triage_error="Rate limited", author="Other Author")
    make_keyword("grouped phrase", group_name="Training", suggested_key="grouped-phrase", scanned_total=90)
    with session_scope() as db:
        inbox_service.decide(db, [accepted], "accept")
        inbox_service.decide(db, [discarded], "discard", discard_reason="Agency")


def _finished_run(**fields) -> int:  # noqa: ANN003
    fields.setdefault("keywords_planned", 1)
    fields.setdefault("keywords_done", 1)
    fields.setdefault("posts_seen", 3)
    fields.setdefault("posts_new", 2)
    fields.setdefault("posts_dupe", 1)
    fields.setdefault("enquiries_triaged", 2)
    keywords = fields.pop("keywords", [("ai workshop", "done")])
    return make_run(fields.pop("status", "succeeded"), keywords=keywords, **fields)


class _Blocker:
    """Holds the single-flight job runner busy with a named job until released."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def hold(self, name: str) -> None:
        started = threading.Event()

        def job() -> None:
            started.set()
            self.release.wait(30)

        get_runner().submit(name, job)  # type: ignore[arg-type]
        assert started.wait(5), f"{name} job did not start"

    def stop(self) -> None:
        self.release.set()
        get_runner().wait(10)


def _seed(name: str, blocker: _Blocker, monkeypatch: pytest.MonkeyPatch) -> State:
    if name == "never_checked":
        return State(name, light="LinkedIn not connected", run_id=_finished_run())

    if name == "connected":
        set_session("connected", last_successful_run_at=utcnow() - timedelta(hours=2))
        run_id = _finished_run()
        return State(
            name, light="LinkedIn connected", run_id=run_id, setup_key="Connect again",
            extra_setup=["LinkedIn is connected. Checks and runs reuse this login invisibly in the background — no window opens."],
        )

    if name == "not_connected_error":
        set_session("not_connected", last_error="Login not completed — try again")
        return State(
            name, light="LinkedIn not connected", run_id=_finished_run(),
            extra_setup=['id="linkedin-error"', "Login not completed — try again"],
            extra_runs=["Connect LinkedIn first"],
        )

    if name == "needs_attention":
        set_session("needs_attention", last_error="Checkpoint page at /checkpoint/challenge/")
        run_id = _finished_run(status="aborted", abort_reason="checkpoint",
                               keywords=[("checkpoint-test", "error"), ("ai workshop", "skipped")])
        return State(
            name, light="LinkedIn needs attention", run_id=run_id,
            banner="LinkedIn needs attention — Checkpoint page at /checkpoint/challenge/",
            extra_setup=["Checkpoint page at /checkpoint/challenge/",
                         "Press Connect LinkedIn — the sales0 Chrome window opens on LinkedIn so you can resolve it there."],
            extra_runs=["Last run stopped: LinkedIn checkpoint detected", "Press Connect LinkedIn in Setup"],
        )

    if name == "busy_connect":
        run_id = _finished_run()
        blocker.hold("connect")
        return State(
            name, light="Connecting…", run_id=run_id,
            setup_key="this is one time only.",
            extra_setup=["Waiting…", "Cancel", "automated test software"],
        )

    if name == "busy_check":
        set_session("connected")
        run_id = _finished_run()
        blocker.hold("check")
        return State(name, light="Checking…", run_id=run_id, setup_key="Connect again",
                     extra_setup=["Checking your LinkedIn session in the background…"])

    if name == "active_run":
        set_session("connected")
        finished = _finished_run()
        active = make_run("running", keywords_planned=2, keywords_done=1, posts_seen=4, posts_new=3,
                          current_step="Searching 'ai workshop' (2/2) — scrolling",
                          keywords=[("ai consulting", "done"), ("ai workshop", "running")])
        blocker.hold("run")
        return State(
            name, light="Run in progress", run_id=finished, progress_run_id=active,
            runs_key="Run in progress", setup_key="Connect again",
            extra_runs=["Runs in the background — no browser window", "Searching 'ai workshop' (2/2) — scrolling"],
        )

    if name == "safety_cooldown":
        monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_MIN", "60")
        monkeypatch.setenv("SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN", "15")
        reset_settings()
        set_session("connected")
        stopped = utcnow().replace(second=0, microsecond=0) - timedelta(minutes=10)
        run_id = _finished_run(status="aborted", abort_reason="rate_limited", started_at=stopped - timedelta(minutes=2),
                               finished_at=stopped, keywords=[("ratelimit-test", "error")])
        return State(
            name, light="LinkedIn connected", run_id=run_id, setup_key="Connect again",
            extra_runs=["LinkedIn asked us to slow down — next run allowed after", 'data-reason-code="safety_cooldown"'],
            extra_detail=["Next run allowed after"],
        )

    if name == "layout_drift":
        set_session("connected")
        run_id = _finished_run(keywords=[("layout-drift-test", "warning"), ("ai workshop", "done")], keywords_planned=2,
                               keywords_done=2)
        return State(
            name, light="LinkedIn connected", run_id=run_id, setup_key="Connect again",
            extra_runs=['id="run-drift-alert"', "LinkedIn's page layout may have changed — 1 keyword(s)", "Layout warning"],
            extra_detail=['id="run-drift-alert"', f'href="/runs/{run_id}#run-keywords"'],
        )

    if name == "browser_fallback":
        from sales0.scraper import browser

        choice = browser.BrowserChoice(channel="chromium", version="140.0.7339.16",
                                       label="Built-in browser (Chromium)", note=FALLBACK_NOTE)
        monkeypatch.setattr(browser, "resolve_browser", lambda settings=None, *a, **k: choice)
        for module in ("sales0.services.session", "sales0.services.runs"):  # in case a service imported the name
            monkeypatch.setattr(f"{module}.resolve_browser", browser.resolve_browser, raising=False)
        return State(name, light="LinkedIn not connected", run_id=_finished_run(), extra_setup=[FALLBACK_NOTE])

    raise AssertionError(name)


STATES = (
    "never_checked",
    "connected",
    "not_connected_error",
    "needs_attention",
    "busy_connect",
    "busy_check",
    "active_run",
    "safety_cooldown",
    "layout_drift",
    "browser_fallback",
)


@pytest.fixture
def seeded(request: pytest.FixtureRequest, db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, State]]:
    blocker = _Blocker()
    with TestClient(create_app()) as client:
        _seed_inbox()
        state = _seed(request.param, blocker, monkeypatch)
        try:
            yield client, state
        finally:
            blocker.stop()
    reset_runner()


def _get(client: TestClient, path: str, status: int = 200) -> str:
    resp = client.get(path)
    assert resp.status_code == status, f"GET {path} → {resp.status_code}\n{resp.text[:2000]}"
    assert "Traceback" not in resp.text and "Something went wrong" not in resp.text, f"GET {path}"
    return unescape(resp.text)


def _assert_contains(html: str, needles: Callable[[], list[str]] | list[str], where: str) -> None:
    for needle in needles() if callable(needles) else needles:
        assert needle in html, f"{where}: missing {needle!r}"


@pytest.mark.parametrize("seeded", STATES, indirect=True)
def test_every_page_renders_200_with_its_key_text(seeded: tuple[TestClient, State]) -> None:
    client, state = seeded
    where = lambda path: f"[{state.name}] {path}"  # noqa: E731

    def page(path: str, key: str, extra: list[str] | None = None) -> str:
        html = _get(client, path)
        _assert_contains(html, [key, 'id="status-light"', state.light, *(extra or [])], where(path))
        if state.banner:
            _assert_contains(html, ['id="alert-banner"', state.banner], where(path))
        else:
            assert 'id="alert-banner"' not in html, where(path)
        return html

    for path in INBOX_PAGES:
        page(path, "Review inbox")
    review = _get(client, "/inbox?tab=review")
    assert 'data-verdict="genuine"' in review, where("/inbox?tab=review")
    assert 'data-review-status="accepted"' in _get(client, "/inbox?tab=accepted"), where("accepted")
    assert 'data-review-status="discarded"' in _get(client, "/inbox?tab=discarded"), where("discarded")
    assert 'article id="skipped-' in _get(client, "/inbox?tab=skipped"), where("skipped")
    assert "AI triage failed — Rate limited" in _get(client, "/inbox?tab=skipped&state=failed"), where("skipped failed")

    setup = page("/setup", state.setup_key, ['id="browser-note"', "Cool-down after rate limit / captcha", *state.extra_setup])
    assert "Browser window during runs" not in setup, where("/setup")
    _assert_contains(setup, ['id="add-suggested-keywords"', 'data-group="Training"', "No yield"], where("/setup"))
    page("/runs", state.runs_key, ['id="run-panel"', 'id="run-history"', *state.extra_runs])
    if state.name == "active_run":
        _assert_contains(_get(client, "/runs"), ['id="run-summary"', "Keyword 2/2"], where("/runs summary"))
    page(f"/runs/{state.run_id}", f"Run #{state.run_id}", ['id="run-keywords"', 'id="run-summary"', *state.extra_detail])
    for path, phase in STUB_PAGES.items():
        page(path, f"Coming in Phase {phase}")

    # Phase 3 outreach pages (the acting member is the seeded admin).
    page("/outreach", "Outreach", ['id="cap-usage"', "0 of 40</strong> sent today", 'id="outbox-filters"', "Coming later"])
    page("/outreach/compose", "Compose outreach", ['id="compose-form"', 'id="research-btn"', 'id="draft-btn"'])
    page("/outreach/templates", "Message templates", ['id="template-form"', 'id="variable-help"'])
    page("/outreach/decks", "Decks", ['id="deck-upload-form"', "up to 10 MB"])
    _assert_contains(_get(client, "/outreach/compose/defaults?offering_id=1"), ['id="template-deck-fields"'], where("defaults"))

    error = _get(client, "/runs/999999", status=404)
    _assert_contains(error, ["That item no longer exists", "Back to sales0"], where("/runs/999999"))

    light = _get(client, "/_shell/status-light")
    _assert_contains(light, ['id="status-light"', state.light], where("/_shell/status-light"))
    banner = _get(client, "/_shell/banner")
    if state.banner:
        _assert_contains(banner, [state.banner], where("/_shell/banner"))
    else:
        assert banner.strip() == "", where("/_shell/banner")
    card = _get(client, "/setup/linkedin/status")
    _assert_contains(card, ['id="linkedin-card"', 'id="browser-note"'], where("/setup/linkedin/status"))
    progress = _get(client, f"/runs/{state.progress_run_id or state.run_id}/progress")
    _assert_contains(progress, ['id="run-panel"'], where("progress"))


def _api_routes(routes: list, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Flatten ``app.routes`` (FastAPI ≥0.16x keeps included routers as nested branches)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):  # fastapi.routing._IncludedRouter
            sub_prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from _api_routes(route.original_router.routes, prefix + sub_prefix)


def test_route_list_covers_every_parameter_free_html_get_route(db_url: str) -> None:
    app = create_app()
    html_gets = {
        path
        for path, route in _api_routes(app.routes)
        if "GET" in route.methods and "{" not in path and not path.startswith("/api/")
    }
    missing = html_gets - COVERED_PARAMLESS
    assert not missing, f"new page route(s) not covered by test_every_page_renders: {sorted(missing)}"
    assert {"/inbox", "/setup", "/runs", "/pipeline"} <= html_gets

"""Recording searcher / LLM and DB readers shared by the pipeline tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any


from sales0.db.engine import session_scope
from sales0.db.models import ScrapeRun
from sales0.jobs import discovery_job
from sales0.linkedin_fixture.bulk import expected_verdict, generate_bulk, results_for, variant_for
from sales0.scraper.search import LinkedInSearcher
from sales0.services import keywords
from sales0.services.keyword_library import SUGGESTED_KEYWORDS
from tests.support.fake_llm import FakeLLMClient

# The first six library phrases the bulk fixture serves (not the four legacy P-set keywords),
# in library order: a mix of both markup variants.
BULK_KEYWORDS: tuple[str, ...] = tuple(k.text for k in SUGGESTED_KEYWORDS if results_for(k.text) is not None)[:6]
VARIANTS = {kw.lower(): variant_for(kw) for kw in BULK_KEYWORDS}


class Killed(BaseException):
    """Simulates the process dying mid-run (not an ``Exception``: the job cannot catch it)."""


class Recorder:
    """Shared, ordered event log: ("scan", kw), ("triage",), ("capture", kw, refs)."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.scanned_text: dict[str, str] = {}  # source_ref -> post text, from every scan
        self.scanned_author: dict[str, str] = {}  # source_ref -> author name
        self.scan_order: list[str] = []
        self.menu_opens = 0
        self.menu_clicks: set[str] = set()
        self.capture_attempts: list[int] = []
        self.kill_on_scan: int | None = None
        self._lock = threading.Lock()

    def add(self, event: tuple) -> None:
        with self._lock:
            self.events.append(event)


class RecordingLLM(FakeLLMClient):
    recorder: Recorder

    def structured(self, req):  # type: ignore[no-untyped-def]
        self.recorder.add(("triage",))
        return super().structured(req)


def oracle_llm(recorder: Recorder) -> RecordingLLM:
    llm = RecordingLLM.with_oracle(expected_verdict)
    llm.recorder = recorder
    return llm


def searcher_factory(recorder: Recorder):  # type: ignore[no-untyped-def]
    class RecordingSearcher(LinkedInSearcher):
        def scan(self, keyword: str, max_posts: int):  # type: ignore[no-untyped-def]
            recorder.scan_order.append(keyword)
            if recorder.kill_on_scan is not None and len(recorder.scan_order) == recorder.kill_on_scan:
                raise Killed("process killed")
            recorder.add(("scan", keyword))
            result = super().scan(keyword, max_posts)
            for post in result.posts:
                recorder.scanned_text[post.source_ref] = post.text
                recorder.scanned_author[post.source_ref] = post.author_name
            return result

        def capture_links(self, refs):  # type: ignore[no-untyped-def]
            kw = recorder.scan_order[-1]
            recorder.add(("capture", kw, list(refs)))
            result = super().capture_links(refs)
            recorder.capture_attempts.append(result.attempts)
            state: dict[str, Any] = self._page.evaluate(  # type: ignore[union-attr]
                "() => ({opens: window.__fixture_menu_opens, clicks: window.__fixture_menu_clicks})"
            )
            recorder.menu_opens += int(state["opens"] or 0)
            recorder.menu_clicks |= set(state["clicks"] or [])
            return result

    return lambda settings: RecordingSearcher(settings)


def add_bulk_keywords(texts: tuple[str, ...] = BULK_KEYWORDS) -> None:
    with session_scope() as db:
        for t in texts:
            keywords.add_keyword(db, t)


def queue_run() -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="fixture", status="queued", started_at=datetime.now(UTC))
        db.add(run)
        db.flush()
        return run.id


def run_discovery(recorder: Recorder, llm: FakeLLMClient) -> int:
    run_id = queue_run()
    discovery_job.execute(run_id, llm=llm, searcher_factory=searcher_factory(recorder), sleep=lambda _s: None)
    return run_id


def genuine_authors(authors: dict[str, str]) -> set[str]:
    """Authors (one bulk post each: "Bulk Author NNN") among the scanned posts whose post is truly genuine."""
    truth = {p.author_name: p.expected for p in generate_bulk()}
    return {a for a in authors.values() if truth.get(a) == "genuine"}

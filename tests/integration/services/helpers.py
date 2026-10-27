"""Shared builders for services/jobs integration tests (real SQLite file, migrated to head)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, LinkedInSession, ScrapeRun
from sales0.scraper.errors import AccountSafetyStop, ScrapeError, SessionMissing
from sales0.scraper.types import KeywordScrapeResult, ScrapedPost
from sales0.services import dedupe
from tests.support.fake_llm import triage_answer


LIVE = "https://www.linkedin.com"
SEARCH_URL = f"{LIVE}/search/results/content/?keywords=ai%20workshop"


def post(n: int, text: str, *, author: str = "Author", headline: str | None = "Head of L&D") -> ScrapedPost:
    """A URN-strategy post (Phase 1.1 shape): the source ref is the URN, the link its permalink."""
    urn = f"urn:li:activity:73000000000000000{n:02d}"
    return ScrapedPost(
        source_ref=urn,
        url=f"{LIVE}/feed/update/{urn}/",
        author_name=author,
        author_profile_url=None,
        author_headline=headline,
        posted_relative="1d",
        posted_date=date(2026, 10, 26),
        text=text,
        link_kind="post",
        search_url=SEARCH_URL,
    )


def sdui_post(
    key: str,
    text: str,
    *,
    slug: str = "in/priya-menon-fx",
    author: str = "Priya Menon",
    urn: str | None = None,
    link: str | None = None,
    search_url: str = SEARCH_URL,
) -> ScrapedPost:
    """An SDUI-strategy post (Phase 1.2): ref ``ck:<key>`` unless a URN was learned (then the URN),
    linked to ``link`` / the URN permalink when known, else to the author profile."""
    profile = f"{LIVE}/{slug}/" if slug else None
    if urn is not None:
        ref, url, kind = urn, f"{LIVE}/feed/update/{urn}/", "post"
    elif link is not None:
        ref, url, kind = f"ck:{key}", link, "post"
    elif profile is not None:
        ref, url, kind = f"ck:{key}", profile, "profile"
    else:
        ref, url, kind = f"ck:{key}", search_url, "search"
    return ScrapedPost(
        source_ref=ref,
        url=url,
        author_name=author,
        author_profile_url=profile,
        author_headline="Head of L&D",
        posted_relative="2d",
        posted_date=date(2026, 10, 25),
        text=text,
        link_kind=kind,  # type: ignore[arg-type]
        search_url=search_url,
    )


def set_session_status(status: str, last_error: str | None = None) -> None:
    with session_scope() as db:
        row = db.get(LinkedInSession, 1)
        assert row is not None
        row.status = status
        row.last_error = last_error


def make_run(status: str = "succeeded") -> int:
    with session_scope() as db:
        run = ScrapeRun(trigger="manual", mode="live", status=status)
        db.add(run)
        db.flush()
        return run.id


def make_enquiry(text: str, *, n: int = 1, keyword: str = "ai workshop", run_id: int | None = None) -> int:
    run_id = run_id or make_run()
    with session_scope() as db:
        _kind, eid = dedupe.upsert_post(db, post(n, text), run_id, None, keyword)
        return eid


def set_triaged(enquiry_id: int, **answer: Any) -> None:
    """Write an AI outcome directly (for inbox tests that are not about triage)."""
    data = triage_answer(**answer)
    with session_scope() as db:
        e = db.get(Enquiry, enquiry_id)
        assert e is not None
        e.triage_status = "done"
        e.ai_verdict = data["verdict"]
        e.ai_reason = data["reason"]
        e.ai_poster_type = data["poster_type"]
        e.ai_priority = data["priority"]
        e.ai_offering_id = data["offering_id"]
        e.requirement_summary = data["requirement_summary"]
        e.priority = data["priority"]
        e.offering_id = data["offering_id"]
        e.company = data["company"]


@dataclass
class FakeSearcher:
    """Stand-in for LinkedInSearcher in job tests: keyword -> posts, or an exception to raise."""

    results: dict[str, list[ScrapedPost] | Exception]
    enter_error: Exception | None = None
    searched: list[str] = field(default_factory=list)
    drift: set[str] = field(default_factory=set)
    # Phase 1.1: keyword -> extra KeywordScrapeResult fields (cards_found, no_results,
    # field_misses, parse_errors; Phase 1.2: strategy, permalink_attempts, permalinks_found)
    # merged into the scripted result.
    extra: dict[str, dict[str, Any]] = field(default_factory=dict)
    closed: bool = False

    def __call__(self, _settings: Any) -> FakeSearcher:
        return self

    def __enter__(self) -> FakeSearcher:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def search(self, keyword: str, max_posts: int) -> KeywordScrapeResult:
        self.searched.append(keyword)
        item = self.results.get(keyword.lower(), [])
        if isinstance(item, Exception):
            raise item
        fields: dict[str, Any] = {"parse_errors": 0, "drift_warning": keyword in self.drift}
        fields.update(self.extra.get(keyword.lower(), {}))
        return KeywordScrapeResult(keyword=keyword, posts=list(item)[:max_posts], scrolls=1, **fields)


def safety(kind: str) -> AccountSafetyStop:
    return AccountSafetyStop(kind, "http://127.0.0.1/__fixtures/linkedin/checkpoint/challenge/")  # type: ignore[arg-type]


def session_missing() -> SessionMissing:
    return SessionMissing("http://127.0.0.1/__fixtures/linkedin/feed/")


def scrape_error(msg: str = "timeout loading results") -> ScrapeError:
    return ScrapeError(msg)


def ids(items: Iterable[Any]) -> list[int]:
    return [i.id for i in items]

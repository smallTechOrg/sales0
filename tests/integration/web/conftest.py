"""Web-layer integration fixtures: a TestClient on a fresh migrated SQLite file, a real
uvicorn server in fixture mode (for the full Run-now journey), and small ORM seeders for
states that are expensive to reach through the scraper (aborted runs, decided cards)."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from sales0.services.session import get_session_row
from sales0.clock import utcnow
from sales0.config import REPO_ROOT, get_settings, reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    EnquirySource,
    LlmCall,
    ScrapeRun,
    Keyword,
    ScrapeRunKeyword,
    SeenPost,
)
from sales0.jobs.runner import reset_runner
from sales0.services.dedupe import content_hash
from sales0.web.app import create_app
from tests.support.ports import free_port

_free_port = free_port  # re-exported for test_qa_hardening

HX = {"HX-Request": "true"}
CORPUS_PATH = REPO_ROOT / "src" / "sales0" / "linkedin_fixture" / "posts.json"


def corpus() -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in json.loads(Path(CORPUS_PATH).read_text(encoding="utf-8"))}


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c
    reset_runner()


# ---------------------------------------------------------------------- seeders
def set_session(status: str, *, last_error: str | None = None, last_successful_run_at: datetime | None = None) -> None:
    with session_scope() as db:
        row = get_session_row(db)
        assert row is not None
        row.status = status
        row.last_error = last_error
        if last_successful_run_at is not None:
            row.last_successful_run_at = last_successful_run_at


def make_run(status: str = "succeeded", *, keywords: list[tuple[str, str]] | None = None, **fields: Any) -> int:
    now = utcnow()
    with session_scope() as db:
        run = ScrapeRun(
            trigger="manual",
            mode="live",
            status=status,
            started_at=fields.pop("started_at", now - timedelta(minutes=5)),
            finished_at=fields.pop("finished_at", None if status in ("queued", "running") else now),
            **fields,
        )
        db.add(run)
        db.flush()
        for text, kw_status in keywords or []:
            db.add(ScrapeRunKeyword(run_id=run.id, keyword_text=text, status=kw_status, posts_seen=0, posts_new=0, posts_dupe=0, errors=0))
        return run.id


def make_enquiry(
    post_id: str = "P1",
    *,
    text: str | None = None,
    keyword: str = "ai workshop",
    run_id: int | None = None,
    added_offset_s: int = 0,
    **fields: Any,
) -> int:
    """Insert an enquiry shaped like a scraped corpus post (+ source + keyword hit)."""
    post = corpus()[post_id]
    raw = text or post["text"]
    urn = post["urn"] if text is None else f"urn:li:activity:{abs(hash(raw)) % 10**19:019d}"
    with session_scope() as db:
        e = Enquiry(
            content_hash=content_hash(raw),
            raw_text=raw,
            author_name=post["author_name"],
            author_headline=post["author_headline"],
            source_url=f"https://www.linkedin.com/feed/update/{urn}/",
            date_posted=date.today() - timedelta(days=1),
            date_added=utcnow() + timedelta(seconds=added_offset_s),
            **fields,
        )
        db.add(e)
        db.flush()
        db.add(EnquirySource(enquiry_id=e.id, source_ref=urn, source_url=e.source_url, first_seen_run_id=run_id, first_seen_at=utcnow()))
        db.add(EnquiryKeywordHit(enquiry_id=e.id, run_id=run_id, keyword_text=keyword, seen_at=utcnow()))
        return e.id


def triaged(verdict: str, reason: str, *, priority: str = "high", offering_id: int | None = 1, summary: str = "Summary", **extra: Any) -> dict[str, Any]:
    """Field values for an enquiry the AI has already triaged (stored as the AI would)."""
    return {
        "triage_status": "done",
        "ai_verdict": verdict,
        "ai_reason": reason,
        "ai_poster_type": extra.pop("poster_type", "buyer_company" if verdict == "genuine" else "training_agency"),
        "ai_priority": priority,
        "ai_offering_id": offering_id,
        "priority": priority,
        "offering_id": offering_id,
        "requirement_summary": summary,
        **extra,
    }


_TRIAGE_AI_RESULT = {
    "verdict": "discard", "reason": "Training agency sourcing trainers", "poster_type": "training_agency",
    "requirement_summary": "Agency - trainers wanted", "priority": "low", "offering_id": None,
}


def make_seen_post(
    text: str = "Hiring freelance AI trainers for upcoming corporate batches, rates per day, apply now.",
    *,
    verdict: str | None = "discard",
    reason: str | None = "Agency recruiting freelance trainers",
    triage_status: str = "done",
    triage_error: str | None = None,
    purged: bool = False,
    author: str | None = "Agency Person",
    post_url: str | None = None,
    times_seen: int = 1,
    keyword_text: str = "ai workshop",
    run_id: int | None = None,
    **fields: Any,
) -> int:
    """Insert a ``seen_posts`` row as discovery leaves it for a post the AI skipped (or failed on)."""
    import hashlib

    now = utcnow()
    with session_scope() as db:
        row = SeenPost(
            fingerprint=hashlib.sha256(f"{author}|{text}".encode()).hexdigest(),
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text_prefix=text[:300],
            author_name=author,
            author_headline="Talent partner",
            keyword_text=keyword_text,
            first_run_id=run_id,
            triage_status=triage_status,
            triage_error=triage_error,
            verdict=verdict,
            reason=reason,
            poster_type="training_agency" if verdict else None,
            ai_result={**_TRIAGE_AI_RESULT, "verdict": verdict, "reason": reason} if verdict else None,
            snippet=" ".join(text.split())[:240],
            raw_text=None if purged else text,
            raw_purge_after=None if purged else now + timedelta(days=14),
            raw_purged_at=now if purged else None,
            post_url=post_url,
            first_seen_at=now - timedelta(hours=3),
            last_seen_at=now - timedelta(hours=2),
            times_seen=times_seen,
            **fields,
        )
        db.add(row)
        db.flush()
        return row.id


def make_keyword(text: str, *, group_name: str | None = None, suggested_key: str | None = None, **fields: Any) -> int:
    with session_scope() as db:
        row = Keyword(text=text, text_key=text.lower(), group_name=group_name, suggested_key=suggested_key, **fields)
        db.add(row)
        db.flush()
        return row.id


def add_llm_call(cost_usd_micros: int, *, enquiry_id: int | None = None, input_tokens: int = 1000, output_tokens: int = 300) -> None:
    with session_scope() as db:
        db.add(
            LlmCall(
                feature="triage",
                model=get_settings().llm_model,
                enquiry_id=enquiry_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd_micros=cost_usd_micros,
                latency_ms=900,
                status="ok",
            )
        )


def oob_fragment(html: str, element_id: str) -> str:
    """The outer HTML of the first element with ``id=element_id`` (rough, for assertions)."""
    match = re.search(rf'<[^>]*id="{re.escape(element_id)}"', html)
    assert match, f"#{element_id} not in response"
    return html[match.start() :]


# ---------------------------------------------------------------------- live fixture-mode server
@pytest.fixture
def fixture_server(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real app on a free port in fixture mode, so the scraper's fake LinkedIn is the
    same server (``base_url`` = http://127.0.0.1:<port>/__fixtures/linkedin)."""
    port = free_port()
    monkeypatch.setenv("SALES0_LINKEDIN_MODE", "fixture")
    monkeypatch.setenv("SALES0_HOST", "127.0.0.1")
    monkeypatch.setenv("SALES0_PORT", str(port))
    reset_settings()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="sales0-web-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("sales0 test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)
    reset_runner()

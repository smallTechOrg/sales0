"""Per-keyword yield: how many posts each keyword scanned and how many turned out genuine.

Derived from today's schema (``scrape_run_keywords.posts_seen`` and ``enquiry_keyword_hits``
joined to genuine enquiries). All SQL lives in :func:`_load_raw`, so a future per-keyword
stats table can replace it without touching the rest. Missing/empty tables yield ``[]``-like
results instead of errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc
from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    Keyword,
    ScrapeRun,
    ScrapeRunKeyword,
)
from sales0.logging import get_logger

log = get_logger("sales0.keyword_yield")


@dataclass(frozen=True)
class KeywordYield:
    keyword_id: (
        int | None
    )  # None: the keyword was deleted; only its text snapshot remains
    text: str
    active: bool
    scanned: int
    relevant: int
    relevant_pct: float
    runs: int
    last_run_at: datetime | None


@dataclass
class RawYield:
    """Unaggregated facts; the single contract between the SQL and the aggregation."""

    keywords: list[tuple[int, str, bool]]  # id, text, active
    run_rows: list[
        tuple[int | None, str, int, int, datetime | None]
    ]  # keyword_id, text, posts_seen, run_id, run start
    genuine_hits: list[tuple[int | None, str, int]]  # keyword_id, text, enquiry_id


def _load_raw(db: Session, since: datetime | None) -> RawYield:
    keywords = [
        (k.id, k.text, k.active)
        for k in db.scalars(select(Keyword).order_by(Keyword.id))
    ]
    run_q = select(
        ScrapeRunKeyword.keyword_id, ScrapeRunKeyword.keyword_text, ScrapeRunKeyword.posts_seen,
        ScrapeRunKeyword.run_id, ScrapeRun.started_at,
    ).join(ScrapeRun, ScrapeRun.id == ScrapeRunKeyword.run_id).where(
        ~ScrapeRunKeyword.keyword_text.like("watch:%")  # prospect-watch profile visits are not keywords
    )  # fmt: skip
    hit_q = (
        select(
            EnquiryKeywordHit.keyword_id,
            EnquiryKeywordHit.keyword_text,
            EnquiryKeywordHit.enquiry_id,
        )
        .join(Enquiry, Enquiry.id == EnquiryKeywordHit.enquiry_id)
        .where(Enquiry.ai_verdict == "genuine", ~EnquiryKeywordHit.keyword_text.like("watch:%"))
        .distinct()
    )
    if since is not None:
        run_q = run_q.where(ScrapeRun.started_at >= since)
        hit_q = hit_q.where(EnquiryKeywordHit.seen_at >= since)
    return RawYield(
        keywords=keywords,
        run_rows=[(r[0], r[1], int(r[2] or 0), r[3], r[4]) for r in db.execute(run_q)],
        genuine_hits=[(r[0], r[1], r[2]) for r in db.execute(hit_q)],
    )


def keyword_yield(db: Session, *, since: datetime | None = None) -> list[KeywordYield]:
    """One row per keyword (active first, then by scanned desc), plus deleted keywords seen in history."""
    try:
        with db.begin_nested():
            raw = _load_raw(db, since)
    except SQLAlchemyError:
        log.warning("keyword_yield_unavailable", exc_info=True)
        return []
    return aggregate(raw)


def aggregate(raw: RawYield) -> list[KeywordYield]:
    by_text = {text.strip().lower(): kid for kid, text, _ in raw.keywords}
    info = {kid: (text, active) for kid, text, active in raw.keywords}

    def key(kid: int | None, text: str) -> int | str:
        if kid is not None and kid in info:
            return kid
        mapped = by_text.get(text.strip().lower())
        return mapped if mapped is not None else f"t:{text.strip().lower()}"

    scanned: dict[int | str, int] = {}
    run_ids: dict[int | str, set[int]] = {}
    last: dict[int | str, datetime] = {}
    names: dict[int | str, str] = {}
    for kid, text, seen, run_id, started in raw.run_rows:
        k = key(kid, text)
        names.setdefault(k, text)
        scanned[k] = scanned.get(k, 0) + seen
        run_ids.setdefault(k, set()).add(run_id)
        if started is not None:
            started = ensure_utc(started)
            if k not in last or started > last[k]:
                last[k] = started
    hits: dict[int | str, set[int]] = {}
    for kid, text, enquiry_id in raw.genuine_hits:
        k = key(kid, text)
        names.setdefault(k, text)
        hits.setdefault(k, set()).add(enquiry_id)

    out: list[KeywordYield] = []
    for k in {*info, *scanned, *hits}:
        if isinstance(k, int):
            text, active = info[k]
            kid: int | None = k
        else:
            text, active, kid = names[k], False, None
        s, r = scanned.get(k, 0), len(hits.get(k, ()))
        out.append(
            KeywordYield(kid, text, active, s, r, round(100.0 * r / s, 1) if s else 0.0, len(run_ids.get(k, ())), last.get(k))
        )  # fmt: skip
    out.sort(key=lambda y: (not y.active, -y.scanned, y.text.lower()))
    return out


__all__ = ["KeywordYield", "RawYield", "aggregate", "keyword_yield"]

"""Flags: repeat poster, competitor mention, no follow-up. One active flag per (entity, kind)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import Competitor, CompetitorMention, Enquiry, Flag, Prospect
from sales0.db.models.enums_crm import FLAG_ENTITY_TYPES, FLAG_KINDS
from sales0.services._common import now_or
from sales0.services.crm import _keys, timeline
from sales0.services.crm.actor import SYSTEM, TEAM, Actor
from sales0.services.crm.views import FlagView

FLAG_LABELS = {
    "repeat_poster": "Repeat poster",
    "competitor_mention": "Mentions a competitor",
    "no_follow_up": "Do not follow up",
}


def _view(f: Flag) -> FlagView:
    return FlagView(kind=f.kind, source=f.source, detail=f.detail, created_at=f.created_at)


def _active(db: Session, entity_type: str, entity_id: int, kind: str) -> Flag | None:
    return db.scalar(
        select(Flag).where(Flag.entity_type == entity_type, Flag.entity_id == entity_id, Flag.kind == kind, Flag.cleared_at.is_(None))
    )


def set_flag(
    db: Session, entity_type: str, entity_id: int, kind: str, *, source: str = "auto",
    detail: dict | None = None, actor: Actor = SYSTEM, now: datetime | None = None,
) -> FlagView:
    """Idempotent: an active flag is kept (its detail updated; a ``user`` flag stays ``user``)."""
    if entity_type not in FLAG_ENTITY_TYPES or kind not in FLAG_KINDS or source not in ("auto", "user"):
        raise ValueError(f"bad flag {entity_type!r}/{kind!r}/{source!r}")
    stamp = now_or(now)
    flag = _active(db, entity_type, entity_id, kind)
    if flag is None:
        flag = Flag(entity_type=entity_type, entity_id=entity_id, kind=kind, source=source, detail=detail,
                    created_at=stamp, updated_at=stamp)
        db.add(flag)
        db.flush()
        timeline.add_event(
            db, subject_type=entity_type, subject_id=entity_id, kind="flag_set",
            body=f"Flag set: {FLAG_LABELS[kind]}", data={"flag": kind, "source": source, **({"detail": detail} if detail else {})},
            actor=actor, occurred_at=stamp,
        )
    else:
        if detail is not None and flag.detail != detail:
            flag.detail = detail
        if source == "user" and flag.source != "user":
            flag.source = "user"
        flag.updated_at = stamp
        db.flush()
    if kind == "no_follow_up" and entity_type == "prospect":
        from sales0.services.crm import followups

        followups.cancel_open(db, prospect_id=entity_id, reason="no_follow_up", actor=actor, now=now)
    return _view(flag)


def clear_flag(
    db: Session, entity_type: str, entity_id: int, kind: str, *, actor: Actor = TEAM, now: datetime | None = None
) -> bool:
    flag = _active(db, entity_type, entity_id, kind)
    if flag is None:
        return False
    stamp = now_or(now)
    flag.cleared_at, flag.cleared_by, flag.updated_at = stamp, actor.label[:80], stamp
    db.flush()
    timeline.add_event(
        db, subject_type=entity_type, subject_id=entity_id, kind="flag_cleared",
        body=f"Flag cleared: {FLAG_LABELS[kind]}", data={"flag": kind}, actor=actor, occurred_at=stamp,
    )
    return True


def flags_for(db: Session, entity_type: str, ids: Sequence[int]) -> dict[int, list[FlagView]]:
    out: dict[int, list[FlagView]] = {}
    if not ids:
        return out
    for f in db.scalars(
        select(Flag).where(Flag.entity_type == entity_type, Flag.entity_id.in_(list(ids)), Flag.cleared_at.is_(None)).order_by(Flag.id)
    ):
        out.setdefault(f.entity_id, []).append(_view(f))
    return out


def has_active(db: Session, entity_type: str, entity_id: int, kind: str) -> bool:
    return _active(db, entity_type, entity_id, kind) is not None


# ------------------------------------------------------------------------------ repeat poster
def recompute_repeat_poster(db: Session, prospect_id: int, *, now: datetime | None = None) -> FlagView | None:
    s = get_settings()
    stamp = now_or(now)
    p = db.get(Prospect, prospect_id)
    if p is None:
        return None
    cond = [Enquiry.prospect_id == p.id]
    if p.linkedin_profile_id:
        cond.append(Enquiry.author_profile_id == p.linkedin_profile_id)
    since = stamp - timedelta(days=s.repeat_poster_days)
    n = int(
        db.scalar(
            select(func.count(Enquiry.id)).where(
                or_(*cond), Enquiry.review_status != "discarded", Enquiry.sheet_removed_at.is_(None), Enquiry.date_added >= since
            )
        )
        or 0
    )
    if n >= s.repeat_poster_min:
        return set_flag(db, "prospect", p.id, "repeat_poster", source="auto",
                        detail={"count": n, "within_days": s.repeat_poster_days}, actor=SYSTEM, now=stamp)
    active = _active(db, "prospect", p.id, "repeat_poster")
    if active is not None and active.source == "auto":
        clear_flag(db, "prospect", p.id, "repeat_poster", actor=SYSTEM, now=stamp)
    return None


# ------------------------------------------------------------------------------ competitors
def _patterns(db: Session) -> list[tuple[int, str, re.Pattern[str]]]:
    out: list[tuple[int, str, re.Pattern[str]]] = []
    for c in db.scalars(select(Competitor).where(Competitor.active.is_(True)).order_by(Competitor.id)):
        terms = {c.name.casefold().strip()}
        host = _keys.website_host(c.website) if c.website else None
        if host:
            terms.add(host.casefold())
        terms = {t for t in terms if len(t) >= 3}
        if terms:
            alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
            out.append((c.id, c.name, re.compile(rf"(?<!\w)(?:{alt})(?!\w)")))
    return out


def sync_competitor_flag(db: Session, entity_type: str, entity_id: int, *, source: str = "auto", now: datetime | None = None) -> None:
    """Derive the ``competitor_mention`` flag of an entity from its ``competitor_mentions`` rows."""
    rows = db.execute(
        select(Competitor.id, Competitor.name)
        .join(CompetitorMention, CompetitorMention.competitor_id == Competitor.id)
        .where(CompetitorMention.entity_type == entity_type, CompetitorMention.entity_id == entity_id, Competitor.active.is_(True))
        .order_by(Competitor.id)
    ).all()
    if rows:
        set_flag(db, entity_type, entity_id, "competitor_mention", source=source,
                 detail={"competitor_ids": [r[0] for r in rows], "names": [r[1] for r in rows]}, now=now)
    else:
        active = _active(db, entity_type, entity_id, "competitor_mention")
        if active is not None and active.source == "auto":
            clear_flag(db, entity_type, entity_id, "competitor_mention", actor=SYSTEM, now=now)


def _sync_prospect_competitor_flag(db: Session, prospect_id: int, now: datetime | None) -> None:
    rows = db.execute(
        select(Competitor.id, Competitor.name)
        .join(CompetitorMention, CompetitorMention.competitor_id == Competitor.id)
        .join(Enquiry, (CompetitorMention.entity_type == "enquiry") & (CompetitorMention.entity_id == Enquiry.id))
        .where(Enquiry.prospect_id == prospect_id, Competitor.active.is_(True))
        .group_by(Competitor.id)
        .order_by(Competitor.id)
    ).all()
    if rows:
        set_flag(db, "prospect", prospect_id, "competitor_mention", source="auto",
                 detail={"competitor_ids": [r[0] for r in rows], "names": [r[1] for r in rows]}, now=now)
    else:
        active = _active(db, "prospect", prospect_id, "competitor_mention")
        if active is not None and active.source == "auto":
            clear_flag(db, "prospect", prospect_id, "competitor_mention", actor=SYSTEM, now=now)


def detect_competitor_mentions(
    db: Session, enquiry_id: int, *, _patterns_cache: list[tuple[int, str, re.Pattern[str]]] | None = None,
    now: datetime | None = None,
) -> list[int]:
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        return []
    patterns = _patterns_cache if _patterns_cache is not None else _patterns(db)
    haystack = " ".join(x for x in (e.raw_text, e.requirement_summary, e.company) if x).casefold()
    matched: dict[int, str] = {}
    for cid, _name, pat in patterns:
        m = pat.search(haystack)
        if m:
            lo = max(m.start() - 60, 0)
            matched[cid] = haystack[lo : m.end() + 60].replace("\n", " ")[:200]
    existing = {
        m.competitor_id: m
        for m in db.scalars(select(CompetitorMention).where(CompetitorMention.entity_type == "enquiry", CompetitorMention.entity_id == e.id))
    }
    changed = False
    for cid, snippet in matched.items():
        if cid not in existing:
            db.add(CompetitorMention(competitor_id=cid, entity_type="enquiry", entity_id=e.id, source="auto",
                                     snippet=snippet, created_at=now_or(now)))
            changed = True
    for cid, m in existing.items():
        if m.source == "auto" and cid not in matched:
            db.execute(delete(CompetitorMention).where(CompetitorMention.id == m.id))
            changed = True
    if changed:
        db.flush()
    sync_competitor_flag(db, "enquiry", e.id, now=now)
    if e.prospect_id:
        _sync_prospect_competitor_flag(db, e.prospect_id, now)
    return sorted(matched)


def on_enquiry_linked(db: Session, enquiry_id: int, *, now: datetime | None = None) -> None:
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        return
    if e.prospect_id:
        recompute_repeat_poster(db, e.prospect_id, now=now)
    detect_competitor_mentions(db, enquiry_id, now=now)


def recompute_competitor_mentions(db: Session, *, now: datetime | None = None) -> None:
    """Re-run mention detection for every enquiry (after the competitor list changed)."""
    stamp = now_or(now)
    patterns = _patterns(db)
    for eid in list(db.scalars(select(Enquiry.id))):
        detect_competitor_mentions(db, eid, _patterns_cache=patterns, now=stamp)


def recompute_all(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    stamp = now_or(now)
    # only prospects that can possibly qualify (a recent non-discarded enquiry) or hold a flag need a recount
    since = stamp - timedelta(days=get_settings().repeat_poster_days)
    recent = (Enquiry.review_status != "discarded", Enquiry.sheet_removed_at.is_(None), Enquiry.date_added >= since)
    by_link = select(Enquiry.prospect_id).where(Enquiry.prospect_id.is_not(None), *recent)
    by_profile = select(Prospect.id).where(Prospect.linkedin_profile_id.in_(select(Enquiry.author_profile_id).where(Enquiry.author_profile_id.is_not(None), *recent)))
    flagged = select(Flag.entity_id).where(Flag.entity_type == "prospect", Flag.kind == "repeat_poster", Flag.cleared_at.is_(None))
    cand = set(db.scalars(by_link)) | set(db.scalars(by_profile)) | set(db.scalars(flagged))
    for pid in sorted(cand):
        recompute_repeat_poster(db, pid, now=stamp)
    recompute_competitor_mentions(db, now=stamp)
    counts = dict(
        db.execute(select(Flag.kind, func.count()).where(Flag.cleared_at.is_(None), Flag.kind.in_(("repeat_poster", "competitor_mention"))).group_by(Flag.kind)).all()
    )
    return {"repeat_poster": int(counts.get("repeat_poster", 0)), "competitor_mention": int(counts.get("competitor_mention", 0))}


__all__ = [
    "clear_flag", "detect_competitor_mentions", "flags_for", "has_active", "on_enquiry_linked", "recompute_all", "recompute_competitor_mentions",
    "recompute_repeat_poster", "set_flag", "sync_competitor_flag",
]

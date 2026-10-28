"""Review inbox: list/search, decide (accept/discard/override), edit fields, retry triage."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, EnquiryKeywordHit, EnquirySource, LlmCall, Offering, TriageDecision
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner, job_busy_error
from sales0.llm import LLMAuthError, LLMModelNotFound
from sales0.scraper.urls import link_kind_of
from sales0.services._common import clip, not_found, now_or, record_history
from sales0.services.views import EnquiryCardView, InboxPage, InboxTab

TAB_STATUS: dict[str, str] = {"review": "pending_review", "accepted": "accepted", "discarded": "discarded"}
PRIORITIES = ("high", "medium", "low")
MAX_PAGE_SIZE = 100


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


# ---------------------------------------------------------------------- card assembly
def _cards(db: Session, enquiries: list[Enquiry]) -> list[EnquiryCardView]:
    if not enquiries:
        return []
    ids = [e.id for e in enquiries]

    keywords: dict[int, list[str]] = defaultdict(list)
    for eid, text in db.execute(
        select(EnquiryKeywordHit.enquiry_id, EnquiryKeywordHit.keyword_text)
        .where(EnquiryKeywordHit.enquiry_id.in_(ids))
        .order_by(EnquiryKeywordHit.seen_at, EnquiryKeywordHit.id)
    ).all():
        if text not in keywords[eid]:
            keywords[eid].append(text)

    first_ref: dict[int, str] = {}
    for eid, ref in db.execute(
        select(EnquirySource.enquiry_id, EnquirySource.source_ref)
        .where(EnquirySource.enquiry_id.in_(ids))
        .order_by(EnquirySource.first_seen_at, EnquirySource.id)
    ).all():
        first_ref.setdefault(eid, ref)

    costs: dict[int, tuple[int, int]] = {}
    for eid, cost, tokens in db.execute(
        select(
            LlmCall.enquiry_id,
            func.coalesce(func.sum(LlmCall.cost_usd_micros), 0),
            func.coalesce(func.sum(LlmCall.input_tokens + LlmCall.output_tokens), 0),
        )
        .where(LlmCall.enquiry_id.in_(ids))
        .group_by(LlmCall.enquiry_id)
    ).all():
        costs[int(eid)] = (int(cost), int(tokens))

    latest_override: dict[int, bool] = {}
    for eid, is_override in db.execute(
        select(TriageDecision.enquiry_id, TriageDecision.is_override)
        .where(TriageDecision.enquiry_id.in_(ids))
        .order_by(TriageDecision.decided_at, TriageDecision.id)
    ).all():
        latest_override[eid] = bool(is_override)  # last write wins = latest decision

    offering_ids = {e.offering_id for e in enquiries if e.offering_id is not None}
    names: dict[int, str] = {}
    if offering_ids:
        names = {oid: name for oid, name in db.execute(select(Offering.id, Offering.name).where(Offering.id.in_(offering_ids)))}

    cards: list[EnquiryCardView] = []
    for e in enquiries:
        cost, tokens = costs.get(e.id, (0, 0))
        cards.append(
            EnquiryCardView(
                id=e.id,
                review_status=e.review_status,  # type: ignore[arg-type]
                stage=e.stage,
                triage_status=e.triage_status,  # type: ignore[arg-type]
                triage_error=e.triage_error,
                requirement_summary=e.requirement_summary,
                ai_verdict=e.ai_verdict,  # type: ignore[arg-type]
                ai_reason=e.ai_reason,
                ai_poster_type=e.ai_poster_type,
                priority=e.priority,
                offering_id=e.offering_id,
                offering_name=names.get(e.offering_id) if e.offering_id is not None else None,
                company=e.company,
                contact_person=e.contact_person,
                email=e.email,
                phone=e.phone,
                city=e.city,
                mode=e.mode,
                dates_mentioned=e.dates_mentioned,
                topic=e.topic,
                author_name=e.author_name,
                author_headline=e.author_headline,
                source_url=e.source_url,
                raw_text=e.raw_text,
                date_posted=e.date_posted,
                date_added=e.date_added,
                keywords=keywords.get(e.id, []),
                discard_reason=e.discard_reason,
                is_override=latest_override.get(e.id, False),
                decided_at=e.decided_at,
                cost_usd_micros=cost,
                tokens_total=tokens,
                source_ref=first_ref.get(e.id),
                ai_priority=e.ai_priority,
                ai_offering_id=e.ai_offering_id,
                link_kind=link_kind_of(e.source_url),
                posted_relative=e.posted_relative,
                author_profile_url=e.author_profile_url,
            )
        )
    return cards


def get_card(db: Session, enquiry_id: int) -> EnquiryCardView:
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None:
        raise not_found()
    return _cards(db, [enquiry])[0]


# ---------------------------------------------------------------------- listing
def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def tab_counts(db: Session) -> dict[str, int]:
    counts: dict[str, int] = {tab: 0 for tab in TAB_STATUS}
    by_status = {status: tab for tab, status in TAB_STATUS.items()}
    for status, n in db.execute(select(Enquiry.review_status, func.count(Enquiry.id)).group_by(Enquiry.review_status)):
        if status in by_status:
            counts[by_status[status]] = int(n)
    from sales0.services.seen_posts import skipped_counts

    skipped = skipped_counts(db)
    counts["skipped"] = skipped["discard"]
    counts["skipped_failed"] = skipped["failed"]
    return counts


def list_enquiries(
    db: Session,
    tab: InboxTab,
    q: str | None,
    page: int = 1,
    page_size: int = 25,
) -> InboxPage:
    if tab not in TAB_STATUS:
        tab = "review"
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or 25), 1), MAX_PAGE_SIZE)
    query = (q or "").strip() or None

    conditions: list[Any] = [Enquiry.review_status == TAB_STATUS[tab]]
    if query:
        pattern = _like(query)
        conditions.append(
            or_(
                Enquiry.raw_text.ilike(pattern, escape="\\"),
                Enquiry.requirement_summary.ilike(pattern, escape="\\"),
                Enquiry.company.ilike(pattern, escape="\\"),
                Enquiry.author_name.ilike(pattern, escape="\\"),
            )
        )

    if tab == "review":
        order = (
            case((Enquiry.triage_status == "failed", 0), else_=1),
            case((Enquiry.ai_verdict == "genuine", 0), (Enquiry.ai_verdict == "discard", 1), else_=2),
            case((Enquiry.priority == "high", 0), (Enquiry.priority == "medium", 1), (Enquiry.priority == "low", 2), else_=3),
            Enquiry.date_added.desc(),
            Enquiry.id.desc(),
        )
    else:
        order = (Enquiry.decided_at.desc(), Enquiry.id.desc())

    total = int(db.scalar(select(func.count(Enquiry.id)).where(*conditions)) or 0)
    rows = list(
        db.scalars(
            select(Enquiry).where(*conditions).order_by(*order).offset((page - 1) * page_size).limit(page_size)
        ).all()
    )
    return InboxPage(
        tab=tab,
        q=query,
        items=_cards(db, rows),
        total=total,
        page=page,
        page_size=page_size,
        counts=tab_counts(db),
    )


# ---------------------------------------------------------------------- decisions
def _field_overrides(e: Enquiry) -> dict[str, list[Any]] | None:
    """Editable values that differ from the AI's original values: {field: [ai, current]}."""
    diff: dict[str, list[Any]] = {}
    if e.ai_verdict is not None:
        if e.priority != e.ai_priority:
            diff["priority"] = [e.ai_priority, e.priority]
        if e.offering_id != e.ai_offering_id:
            diff["offering_id"] = [e.ai_offering_id, e.offering_id]
    return diff or None


def _latest_decision(db: Session, enquiry_id: int) -> TriageDecision | None:
    return db.scalar(
        select(TriageDecision)
        .where(TriageDecision.enquiry_id == enquiry_id)
        .order_by(TriageDecision.decided_at.desc(), TriageDecision.id.desc())
        .limit(1)
    )


def decide(
    db: Session,
    enquiry_ids: list[int],
    decision: Literal["accept", "discard"],
    discard_reason: str | None = None,
    actor_user_id: int | None = None,
    *,
    now: datetime | None = None,
) -> list[EnquiryCardView]:
    ids = list(dict.fromkeys(int(i) for i in (enquiry_ids or [])))
    if not ids or decision not in ("accept", "discard"):
        raise DomainError("decision_invalid", "Choose Accept or Discard for at least one enquiry.", 400)
    rows = {e.id: e for e in db.scalars(select(Enquiry).where(Enquiry.id.in_(ids))).all()}
    if len(rows) != len(ids):
        raise not_found()
    pending = [i for i in ids if rows[i].triage_status == "pending"]
    if pending:
        raise DomainError(
            "decision_invalid",
            "AI triage is still pending for "
            f"{len(pending)} selected enquir{'y' if len(pending) == 1 else 'ies'} — wait for it to finish.",
            400,
        )

    stamp = now_or(now)
    preset = clip(discard_reason, 200)
    for eid in ids:
        e = rows[eid]
        if decision == "accept":
            to_status, to_stage = "accepted", "prospect_new"
            is_override = e.ai_verdict == "discard"
            reason = None
        else:
            to_status, to_stage = "discarded", None
            is_override = e.ai_verdict == "genuine"
            reason = preset or clip(e.ai_reason, 200)
        record_history(
            db, "enquiry", e.id, "review_status", e.review_status, to_status, actor_user_id=actor_user_id, now=stamp
        )
        if e.stage != to_stage:
            record_history(db, "enquiry", e.id, "stage", e.stage, to_stage, actor_user_id=actor_user_id, now=stamp)
        e.review_status = to_status
        e.stage = to_stage
        e.discard_reason = reason if decision == "discard" else None
        e.decided_at = stamp
        e.actor_user_id = actor_user_id
        db.add(
            TriageDecision(
                enquiry_id=e.id,
                decision=decision,
                ai_verdict=e.ai_verdict,
                is_override=is_override,
                reason=reason,
                field_overrides=_field_overrides(e),
                decided_at=stamp,
                actor_user_id=actor_user_id,
            )
        )
    db.flush()
    return _cards(db, [rows[i] for i in ids])


def update_fields(
    db: Session,
    enquiry_id: int,
    priority: str | None = None,
    offering_id: int | None = UNSET,
    *,
    actor_user_id: int | None = None,
) -> EnquiryCardView:
    """Edit priority / offering. ``priority=None`` keeps it; ``offering_id`` omitted keeps it,
    ``offering_id=None`` sets "No match". The AI's original values are never changed; the
    diff is recorded in the latest decision's ``field_overrides`` (and on every later decision)."""
    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    changed: list[tuple[str, Any, Any]] = []
    if priority is not None and priority != "":
        value = priority.strip().lower()
        if value not in PRIORITIES:
            raise DomainError("field_invalid", "Priority must be High, Medium or Low.", 400)
        if value != e.priority:
            changed.append(("priority", e.priority, value))
            e.priority = value
    if offering_id is not UNSET:
        new_id = int(offering_id) if offering_id not in (None, "", 0) else None
        if new_id is not None:
            offering = db.get(Offering, new_id)
            if offering is None:
                raise not_found()
            if not offering.active and new_id != e.offering_id:
                raise DomainError("field_invalid", f"'{offering.name}' is no longer an active offering.", 400)
        if new_id != e.offering_id:
            changed.append(("offering_id", e.offering_id, new_id))
            e.offering_id = new_id
    for field, before, after in changed:
        record_history(
            db,
            "enquiry",
            e.id,
            field,
            None if before is None else str(before),
            None if after is None else str(after),
            actor_user_id=actor_user_id,
        )
    if changed:
        latest = _latest_decision(db, e.id)
        if latest is not None:
            latest.field_overrides = _field_overrides(e)
    db.flush()
    return _cards(db, [e])[0]


def retry_triage(db: Session, enquiry_id: int) -> EnquiryCardView:
    """One synchronous real triage call. The new state is committed even when it fails."""
    from sales0.services.triage import triage_enquiry

    e = db.get(Enquiry, enquiry_id)
    if e is None:
        raise not_found()
    if e.triage_status not in ("failed", "pending"):
        # Retry is only for an enquiry whose AI triage failed or never ran; re-triaging a
        # finished card would silently overwrite its verdict (and spend a call).
        raise DomainError(
            "decision_invalid", "This enquiry is already triaged — Retry triage is only for failed triage.", 400
        )
    busy = get_runner().busy()
    if busy == "run" and e.triage_status == "pending":
        raise job_busy_error("run")
    try:
        outcome = triage_enquiry(db, enquiry_id)
    except LLMAuthError as exc:
        db.commit()
        raise DomainError("llm_auth", exc.message.rstrip(".") + ".", 502) from exc
    except LLMModelNotFound as exc:
        db.commit()
        raise DomainError("triage_failed", f"AI triage failed: {exc.message}. Try again in a minute.", 502) from exc
    db.commit()
    if outcome.status != "done":
        raise DomainError(
            "triage_failed", f"AI triage failed: {(outcome.error or 'unknown error').rstrip('.')}. Try again in a minute.", 502
        )
    return get_card(db, enquiry_id)


__all__ = ["UNSET", "decide", "get_card", "list_enquiries", "retry_triage", "tab_counts", "update_fields"]

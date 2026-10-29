"""Triage criteria (inbox feedback A3): the include / exclude rules of the triage prompt, editable on /criteria.

- ``active_set`` is what every triage path passes to the agent (``TriageDeps.criteria``): the ACTIVE rules in
  prompt order plus the snapshot version (``triage_criteria_sets``). An empty table (a database that never saw the
  q14 seed) answers the built-in defaults with version None, so behaviour never silently changes.
- Every change writes a new immutable snapshot (version + 1), so ``llm_calls.criteria_version`` traces a triage call
  to the exact rule set in its system prompt.
- ``discard_breakdown`` groups discard reasons (team decisions, system auto-discards, AI skips) for the same page.

Functions take ``db`` first and flush, never commit (the request / job commits).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from sales0.agent.criteria import DEFAULT_SET, MAX_RULE_CHARS, CriteriaSet, Criterion
from sales0.db.models import Enquiry, LinkedinProfile, SeenPost, TriageCriteriaSet, TriageCriterion, TriageDecision
from sales0.errors import DomainError
from sales0.scraper.urls import link_kind_of
from sales0.services._common import not_found, now_or

Kind = Literal["include", "exclude"]
KINDS: tuple[str, ...] = ("include", "exclude")
MIN_RULE_CHARS = 5
EXAMPLES_PER_GROUP = 3
MAX_GROUPS = 25
SNIPPET = 180

POSTER_LABELS = {
    "buyer_company": "Buyer company",
    "training_agency": "Training agency",
    "individual_trainer_seeker": "Seeks individual trainer",
    "job_seeker_or_self_promo": "Job seeker / self-promo",
    "other": "Other (events, news, opinion, spam)",
}


# ---------------------------------------------------------------------------- views
@dataclass(frozen=True)
class CriterionView:
    id: int
    kind: str
    text: str
    position: int
    active: bool
    source: str
    from_enquiry_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CriteriaVersionView:
    version: int
    rules: int
    note: str | None
    created_at: datetime


@dataclass(frozen=True)
class ExamplePost:
    snippet: str
    url: str | None
    link_kind: str | None
    at: datetime | None


@dataclass
class ReasonGroup:
    source: Literal["team", "system", "ai"]
    label: str
    count_30d: int = 0
    count_all: int = 0
    examples: list[ExamplePost] = field(default_factory=list)


@dataclass(frozen=True)
class DiscardBreakdown:
    team: list[ReasonGroup]
    system: list[ReasonGroup]
    ai: list[ReasonGroup]

    @property
    def totals(self) -> dict[str, tuple[int, int]]:
        return {
            name: (sum(g.count_30d for g in groups), sum(g.count_all for g in groups))
            for name, groups in (("team", self.team), ("system", self.system), ("ai", self.ai))
        }


def _view(row: TriageCriterion) -> CriterionView:
    return CriterionView(
        id=row.id, kind=row.kind, text=row.text, position=row.position, active=bool(row.active), source=row.source,
        from_enquiry_id=row.from_enquiry_id, created_at=row.created_at, updated_at=row.updated_at,
    )


def _ordered(db: Session, *, active_only: bool = False) -> list[TriageCriterion]:
    stmt = select(TriageCriterion)
    if active_only:
        stmt = stmt.where(TriageCriterion.active.is_(True))
    rows = list(db.scalars(stmt.order_by(TriageCriterion.position, TriageCriterion.id)).all())
    return sorted(rows, key=lambda r: (KINDS.index(r.kind) if r.kind in KINDS else 9, r.position, r.id))


# ---------------------------------------------------------------------------- reads
def current_version(db: Session) -> int | None:
    value = db.scalar(select(func.max(TriageCriteriaSet.version)))
    return int(value) if value is not None else None


def active_set(db: Session) -> CriteriaSet:
    """The rules the next triage call renders (prompt order) and their snapshot version."""
    if db.scalar(select(func.count(TriageCriterion.id))) == 0:
        return DEFAULT_SET
    rules = tuple(Criterion(r.kind, r.text) for r in _ordered(db, active_only=True))  # type: ignore[arg-type]
    return CriteriaSet(rules=rules, version=current_version(db))


def list_rules(db: Session) -> list[CriterionView]:
    return [_view(r) for r in _ordered(db)]


def history(db: Session, limit: int = 10) -> list[CriteriaVersionView]:
    rows = db.scalars(select(TriageCriteriaSet).order_by(TriageCriteriaSet.version.desc()).limit(limit)).all()
    return [CriteriaVersionView(r.version, len(r.rules or []), r.note, r.created_at) for r in rows]


# ---------------------------------------------------------------------------- writes
def _clean(text: str | None) -> str:
    value = " ".join((text or "").split())
    if len(value) < MIN_RULE_CHARS:
        raise DomainError("criterion_invalid", f"Write the rule in a few words (at least {MIN_RULE_CHARS} characters).", 400)
    if len(value) > MAX_RULE_CHARS:
        raise DomainError("criterion_invalid", f"A rule can be at most {MAX_RULE_CHARS} characters.", 400)
    return value


def _kind(kind: str | None) -> str:
    value = (kind or "").strip().lower()
    if value not in KINDS:
        raise DomainError("criterion_invalid", "Choose Include (genuine when…) or Exclude (discard when…).", 400)
    return value


def _snapshot(db: Session, *, actor_member_id: int | None, note: str, now: datetime) -> int:
    db.flush()
    version = (current_version(db) or 0) + 1
    rules = [{"kind": r.kind, "text": r.text} for r in _ordered(db, active_only=True)]
    db.add(TriageCriteriaSet(version=version, rules=rules, note=note[:200], actor_member_id=actor_member_id, created_at=now))
    db.flush()
    return version


def _get(db: Session, criterion_id: int) -> TriageCriterion:
    row = db.get(TriageCriterion, criterion_id)
    if row is None:
        raise not_found()
    return row


def _same_text(db: Session, kind: str, text: str, exclude_id: int | None = None) -> TriageCriterion | None:
    for row in db.scalars(select(TriageCriterion).where(TriageCriterion.kind == kind)).all():
        if row.id != exclude_id and row.text.casefold() == text.casefold():
            return row
    return None


def create_rule(
    db: Session, kind: str, text: str, *, actor_member_id: int | None = None, source: str = "team",
    from_enquiry_id: int | None = None, now: datetime | None = None,
) -> CriterionView:
    """Append an active rule at the end of its list. A rule with the same text is re-enabled instead of duplicated."""
    stamp = now_or(now)
    kind, text = _kind(kind), _clean(text)
    existing = _same_text(db, kind, text)
    if existing is not None:
        if existing.active:
            raise DomainError("criterion_exists", "That rule already exists.", 409)
        existing.active, existing.updated_at, existing.updated_by_member_id = True, stamp, actor_member_id
        _snapshot(db, actor_member_id=actor_member_id, note=f"Re-enabled {kind} rule #{existing.id}", now=stamp)
        return _view(existing)
    last = db.scalar(select(func.max(TriageCriterion.position)).where(TriageCriterion.kind == kind)) or 0
    row = TriageCriterion(
        kind=kind, text=text, position=int(last) + 10, active=True, source=source if source in ("team", "discard") else "team",
        from_enquiry_id=from_enquiry_id, created_by_member_id=actor_member_id, updated_by_member_id=actor_member_id,
        created_at=stamp, updated_at=stamp,
    )
    db.add(row)
    db.flush()
    _snapshot(db, actor_member_id=actor_member_id, note=f"Added {kind} rule #{row.id}", now=stamp)
    return _view(row)


def update_rule(
    db: Session, criterion_id: int, *, text: str | None = None, kind: str | None = None,
    actor_member_id: int | None = None, now: datetime | None = None,
) -> CriterionView:
    stamp = now_or(now)
    row = _get(db, criterion_id)
    new_kind = _kind(kind) if kind is not None else row.kind
    new_text = _clean(text) if text is not None else row.text
    if new_kind == row.kind and new_text == row.text:
        return _view(row)
    if _same_text(db, new_kind, new_text, exclude_id=row.id) is not None:
        raise DomainError("criterion_exists", "Another rule already says that.", 409)
    if new_kind != row.kind:
        last = db.scalar(select(func.max(TriageCriterion.position)).where(TriageCriterion.kind == new_kind)) or 0
        row.position = int(last) + 10
    row.kind, row.text, row.updated_at, row.updated_by_member_id = new_kind, new_text, stamp, actor_member_id
    _snapshot(db, actor_member_id=actor_member_id, note=f"Edited rule #{row.id}", now=stamp)
    return _view(row)


def set_active(
    db: Session, criterion_id: int, active: bool, *, actor_member_id: int | None = None, now: datetime | None = None
) -> CriterionView:
    stamp = now_or(now)
    row = _get(db, criterion_id)
    if bool(row.active) == bool(active):
        return _view(row)
    row.active, row.updated_at, row.updated_by_member_id = bool(active), stamp, actor_member_id
    _snapshot(db, actor_member_id=actor_member_id, note=f"{'Enabled' if active else 'Disabled'} rule #{row.id}", now=stamp)
    return _view(row)


def move(
    db: Session, criterion_id: int, direction: str, *, actor_member_id: int | None = None, now: datetime | None = None
) -> CriterionView:
    """Swap with the neighbour above / below in the same list (positions are renumbered 10, 20, ...)."""
    stamp = now_or(now)
    row = _get(db, criterion_id)
    if direction not in ("up", "down"):
        raise DomainError("criterion_invalid", "Move a rule up or down.", 400)
    same = [r for r in _ordered(db) if r.kind == row.kind]
    i = same.index(row)
    j = i - 1 if direction == "up" else i + 1
    if not 0 <= j < len(same):
        return _view(row)
    same[i], same[j] = same[j], same[i]
    for n, r in enumerate(same, start=1):
        r.position = n * 10
    row.updated_at, row.updated_by_member_id = stamp, actor_member_id
    _snapshot(db, actor_member_id=actor_member_id, note=f"Moved rule #{row.id} {direction}", now=stamp)
    return _view(row)


def rule_from_discard(
    db: Session, reason: str, *, enquiry_id: int | None, actor_member_id: int | None = None, now: datetime | None = None
) -> CriterionView | None:
    """"Save as rule" on a discard: a new EXCLUDE rule worded from the team's reason (None when it already exists)."""
    try:
        return create_rule(db, "exclude", reason, actor_member_id=actor_member_id, source="discard",
                           from_enquiry_id=enquiry_id, now=now)
    except DomainError as exc:
        if exc.code == "criterion_exists":
            return None
        raise


# ---------------------------------------------------------------------------- discard breakdown
def _snippet(text: str | None) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= SNIPPET else line[: SNIPPET - 1].rstrip() + "…"


def _add(groups: OrderedDict[str, ReasonGroup], key: str, group: ReasonGroup, at: datetime | None, since: datetime,
         example: ExamplePost) -> None:
    g = groups.setdefault(key, group)
    g.count_all += 1
    if at is not None and at >= since:
        g.count_30d += 1
    if len(g.examples) < EXAMPLES_PER_GROUP:
        g.examples.append(example)


def _team_groups(db: Session, since: datetime) -> list[ReasonGroup]:
    ranked = select(
        TriageDecision.id.label("decision_id"),
        func.row_number()
        .over(partition_by=TriageDecision.enquiry_id, order_by=(TriageDecision.decided_at.desc(), TriageDecision.id.desc()))
        .label("rn"),
    ).subquery()
    rows = db.execute(
        select(TriageDecision.reason, TriageDecision.decided_at, Enquiry.raw_text, Enquiry.source_url)
        .join(ranked, ranked.c.decision_id == TriageDecision.id)
        .join(Enquiry, Enquiry.id == TriageDecision.enquiry_id)
        .where(ranked.c.rn == 1, TriageDecision.decision == "discard", Enquiry.review_status == "discarded")
        .order_by(TriageDecision.decided_at.desc(), TriageDecision.id.desc())
        .limit(5000)
    ).all()
    groups: OrderedDict[str, ReasonGroup] = OrderedDict()
    for reason, at, raw_text, url in rows:
        label = " ".join((reason or "").split()) or "No reason given"
        _add(groups, label.casefold(), ReasonGroup("team", label), at, since,
             ExamplePost(_snippet(raw_text), url, link_kind_of(url), at))
    return sorted(groups.values(), key=lambda g: (-g.count_all, g.label))[:MAX_GROUPS]


def _system_groups(db: Session, since: datetime) -> list[ReasonGroup]:
    rows = db.execute(
        select(Enquiry.decided_at, Enquiry.raw_text, Enquiry.source_url, Enquiry.discard_reason)
        .where(Enquiry.review_status == "discarded", Enquiry.discard_source == "auto")
        .order_by(Enquiry.decided_at.desc(), Enquiry.id.desc())
        .limit(5000)
    ).all()
    groups: OrderedDict[str, ReasonGroup] = OrderedDict()
    for at, raw_text, url, _reason in rows:
        _add(groups, "repeat", ReasonGroup("system", "Repeat from an existing prospect (more than one in a run)"), at,
             since, ExamplePost(_snippet(raw_text), url, link_kind_of(url), at))
    return list(groups.values())


def _ai_groups(db: Session, since: datetime) -> list[ReasonGroup]:
    condition = (SeenPost.verdict == "discard") & SeenPost.enquiry_id.is_(None)
    counts = db.execute(
        select(
            SeenPost.poster_type,
            func.count(SeenPost.id),
            func.sum(case((SeenPost.first_seen_at >= since, 1), else_=0)),
        )
        .where(condition)
        .group_by(SeenPost.poster_type)
    ).all()
    out: list[ReasonGroup] = []
    for poster_type, total, recent in sorted(counts, key=lambda r: -int(r[1] or 0)):
        g = ReasonGroup("ai", "AI: " + POSTER_LABELS.get(poster_type or "", poster_type or "unclassified"),
                        count_30d=int(recent or 0), count_all=int(total or 0))
        ptype = SeenPost.poster_type.is_(None) if poster_type is None else SeenPost.poster_type == poster_type
        for snippet, reason, post_url, profile_url, at in db.execute(
            select(SeenPost.snippet, SeenPost.reason, SeenPost.post_url, LinkedinProfile.profile_url, SeenPost.first_seen_at)
            .outerjoin(LinkedinProfile, LinkedinProfile.id == SeenPost.profile_id)
            .where(condition, ptype)
            .order_by(SeenPost.first_seen_at.desc(), SeenPost.id.desc())
            .limit(EXAMPLES_PER_GROUP)
        ).all():
            url = post_url or profile_url
            text = _snippet(snippet) + (f" — AI: {reason}" if reason else "")
            g.examples.append(ExamplePost(text, url, link_kind_of(url), at))
        out.append(g)
    return out[:MAX_GROUPS]


def discard_breakdown(db: Session, *, now: datetime | None = None, days: int = 30) -> DiscardBreakdown:
    since = now_or(now) - timedelta(days=days)
    return DiscardBreakdown(team=_team_groups(db, since), system=_system_groups(db, since), ai=_ai_groups(db, since))


__all__ = [
    "CriteriaVersionView",
    "CriterionView",
    "DiscardBreakdown",
    "ReasonGroup",
    "active_set",
    "create_rule",
    "current_version",
    "discard_breakdown",
    "history",
    "list_rules",
    "move",
    "rule_from_discard",
    "set_active",
    "update_rule",
]

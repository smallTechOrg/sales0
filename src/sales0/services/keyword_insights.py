"""Keyword insights: what each search keyword actually brought in, and a performance tier.

Per keyword (``keywords`` totals + ``enquiry_keyword_hits`` joined to the enquiries they found):
- ``scanned`` / ``relevant``: the run counters on the keyword row (posts scanned, posts the AI judged genuine);
- downstream outcomes over the DISTINCT enquiries the keyword found: ``accepted`` (review_status accepted),
  ``contacted`` (an enquiry status past Open), ``leads`` (its company is a lead or client) and ``won`` (enquiry status
  Won or company a client).

Tiers (``TIER_ORDER``), decided in code from those numbers:
- ``winner``: at least ``WINNER_ACCEPTED`` accepted enquiries, or ``WINNER_RELEVANT`` relevant at >= ``WINNER_RATE``;
- ``promising``: at least one relevant post;
- ``new``: fewer than ``NEW_SCANNED`` posts scanned (not enough data yet);
- ``dead``: ``DEAD_SCANNED``+ scanned and nothing relevant;
- ``underperforming``: the rest (enough scanned, nothing relevant yet).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Company, Enquiry, EnquiryKeywordHit, Keyword, Offering

NEW_SCANNED = 30
DEAD_SCANNED = 80
WINNER_ACCEPTED = 2
WINNER_RELEVANT = 3
WINNER_RATE = 0.10
TIER_ORDER = ("winner", "promising", "new", "underperforming", "dead")
TIER_LABELS = {
    "winner": "Winners", "promising": "Promising", "new": "New (not enough data)",
    "underperforming": "Underperforming", "dead": "Dead (no yield)",
}
TIER_HELP = {
    "winner": f"{WINNER_ACCEPTED}+ accepted enquiries, or {WINNER_RELEVANT}+ relevant posts at {int(WINNER_RATE * 100)}%+",
    "promising": "found at least one relevant post",
    "new": f"fewer than {NEW_SCANNED} posts scanned so far",
    "underperforming": f"{NEW_SCANNED}+ scanned, nothing relevant yet",
    "dead": f"{DEAD_SCANNED}+ scanned and nothing relevant: pause, reword or remove",
}
CONTACTED_PAST = ("open",)  # any enquiry status other than these (and empty) means we reached out
SORTS = ("tier", "relevant", "rate", "accepted", "won", "scanned", "text", "recent")
VIEWS = ("tier", "offering", "outcome", "table")


@dataclass
class KeywordStat:
    id: int
    text: str
    active: bool
    offering_id: int | None
    offering_name: str | None
    offering_active: bool
    suggested: bool
    scanned: int
    relevant: int
    last_run_at: datetime | None
    created_at: datetime | None
    found: int = 0
    accepted: int = 0
    contacted: int = 0
    leads: int = 0
    won: int = 0
    tier: str = "new"

    @property
    def rate(self) -> float:
        return self.relevant / self.scanned if self.scanned else 0.0

    @property
    def rate_pct(self) -> str:
        return f"{self.rate * 100:.1f}%" if self.scanned else "-"


@dataclass
class OfferingStat:
    """Enquiries the AI (or a person) matched to the offering, and how far they went."""

    matched: int = 0
    accepted: int = 0
    contacted: int = 0
    leads: int = 0
    won: int = 0


@dataclass
class Group:
    key: str
    label: str
    help: str = ""
    keywords: list[KeywordStat] = field(default_factory=list)
    # set for an offering card (view "offering"): the offering itself and its enquiry results
    offering: Any = None
    offering_stat: OfferingStat | None = None

    @property
    def scanned(self) -> int:
        return sum(k.scanned for k in self.keywords)

    @property
    def relevant(self) -> int:
        return sum(k.relevant for k in self.keywords)

    @property
    def accepted(self) -> int:
        return sum(k.accepted for k in self.keywords)


def tier_of(scanned: int, relevant: int, accepted: int) -> str:
    if accepted >= WINNER_ACCEPTED or (relevant >= WINNER_RELEVANT and scanned and relevant / scanned >= WINNER_RATE):
        return "winner"
    if relevant >= 1:
        return "promising"
    if scanned < NEW_SCANNED:
        return "new"
    if scanned >= DEAD_SCANNED:
        return "dead"
    return "underperforming"


def keyword_stats(db: Session) -> list[KeywordStat]:
    offerings = {o.id: o for o in db.scalars(select(Offering))}
    stats: dict[int, KeywordStat] = {}
    for k in db.scalars(select(Keyword).order_by(Keyword.id)):
        o = offerings.get(k.offering_id) if k.offering_id else None
        stats[k.id] = KeywordStat(
            id=k.id, text=k.text, active=k.active, offering_id=k.offering_id, offering_name=o.name if o else k.group_name,
            offering_active=o.active if o else True, suggested=k.suggested_key is not None, scanned=k.scanned_total or 0,
            relevant=k.relevant_total or 0, last_run_at=k.last_run_at, created_at=k.created_at,
        )
    if stats:
        by_text = {s.text.lower(): s.id for s in stats.values()}
        rows = db.execute(
            select(EnquiryKeywordHit.keyword_id, EnquiryKeywordHit.keyword_text, Enquiry.id, Enquiry.review_status,
                   Enquiry.deal_status, Company.stage)
            .join(Enquiry, Enquiry.id == EnquiryKeywordHit.enquiry_id)
            .outerjoin(Company, Company.id == Enquiry.company_id)
        ).all()
        seen: dict[int, set[int]] = defaultdict(set)
        for kid, ktext, eid, review, deal, stage in rows:
            # a hit names its keyword by id; older / imported hits only carry the text snapshot
            kid = kid if kid in stats else by_text.get((ktext or "").strip().lower())
            if kid is None or eid in seen[kid]:
                continue
            seen[kid].add(eid)
            s = stats[kid]
            s.found += 1
            if review == "accepted":
                s.accepted += 1
            if deal and deal not in CONTACTED_PAST:
                s.contacted += 1
            if stage in ("lead", "client"):
                s.leads += 1
            if deal == "won" or stage == "client":
                s.won += 1
    for s in stats.values():
        s.tier = tier_of(s.scanned, s.relevant, s.accepted)
    return list(stats.values())


def _sort_key(sort: str):  # type: ignore[no-untyped-def]
    tier_rank = {t: i for i, t in enumerate(TIER_ORDER)}
    return {
        "tier": lambda s: (tier_rank[s.tier], -s.accepted, -s.relevant, s.text.lower()),
        "relevant": lambda s: (-s.relevant, -s.rate, s.text.lower()),
        "rate": lambda s: (-s.rate, -s.relevant, s.text.lower()),
        "accepted": lambda s: (-s.accepted, -s.relevant, s.text.lower()),
        "won": lambda s: (-s.won, -s.leads, -s.accepted, s.text.lower()),
        "scanned": lambda s: (-s.scanned, s.text.lower()),
        "text": lambda s: s.text.lower(),
        "recent": lambda s: (-(s.created_at.timestamp() if s.created_at else 0), s.text.lower()),
    }.get(sort, lambda s: (tier_rank[s.tier], -s.accepted, -s.relevant, s.text.lower()))


def filtered(stats: list[KeywordStat], *, q: str = "", tier: str = "", status: str = "", offering: str = "",
             sort: str = "tier") -> list[KeywordStat]:
    q = q.strip().lower()
    out = [
        s for s in stats
        if (not q or q in s.text.lower())
        and (not tier or s.tier == tier)
        and (not status or (status == "active") == s.active)
        and (not offering or (offering == "none" and s.offering_id is None) or str(s.offering_id) == offering)
    ]
    return sorted(out, key=_sort_key(sort))


def offering_stats(db: Session) -> dict[int, OfferingStat]:
    out: dict[int, OfferingStat] = defaultdict(OfferingStat)
    rows = db.execute(
        select(Enquiry.offering_id, Enquiry.review_status, Enquiry.deal_status, Company.stage)
        .outerjoin(Company, Company.id == Enquiry.company_id).where(Enquiry.offering_id.is_not(None))
    ).all()
    for oid, review, deal, stage in rows:
        s = out[int(oid)]
        s.matched += 1
        s.accepted += review == "accepted"
        s.contacted += bool(deal and deal not in CONTACTED_PAST)
        s.leads += stage in ("lead", "client")
        s.won += deal == "won" or stage == "client"
    return dict(out)


def by_offering(stats: list[KeywordStat], offerings: list[Any], ostats: dict[int, OfferingStat], *, include_empty: bool) -> list[Group]:
    """One card per offering in catalogue order (group, then sort order), each with its keywords and its enquiry results,
    then "No offering". ``include_empty`` keeps offerings without (matching) keywords, so they can still be managed."""
    groups: list[Group] = []
    by_id: dict[int | None, list[KeywordStat]] = defaultdict(list)
    for s in stats:
        by_id[s.offering_id].append(s)
    ordered = sorted(offerings, key=lambda o: (getattr(o.group, "sort_order", 0), getattr(o.group, "name", "").lower(), o.sort_order, o.id))
    for o in ordered:
        kws = by_id.pop(o.id, [])
        if not kws and not include_empty:
            continue
        groups.append(Group(str(o.id), o.name, "" if o.active else "offering off - its keywords are skipped in runs", kws,
                            offering=o, offering_stat=ostats.get(o.id, OfferingStat())))
    rest = [s for kws in by_id.values() for s in kws]  # no offering, or an offering that no longer exists
    if rest:
        groups.append(Group("none", "No offering", "keywords not tied to an offering", rest))
    return groups


def grouped(stats: list[KeywordStat], view: str) -> list[Group]:
    """``tier``: one group per tier (fixed order); ``offering``: per offering, then "No offering"; ``outcome``: keywords
    that led to won / leads / contacted / accepted enquiries, then the ones with nothing downstream; ``table``: one."""
    if view == "offering":
        groups: dict[str, Group] = {}
        for s in stats:
            key = str(s.offering_id) if s.offering_id else "none"
            g = groups.setdefault(key, Group(key, s.offering_name or "No offering",
                                             "" if s.offering_active else "offering off - its keywords are skipped in runs"))
            g.keywords.append(s)
        return sorted(groups.values(), key=lambda g: (g.key == "none", g.label.lower()))
    if view == "outcome":
        buckets = [Group("won", "Led to a client / won deal"), Group("leads", "Led to a lead"),
                   Group("contacted", "Led to contacted enquiries"), Group("accepted", "Led to accepted enquiries"),
                   Group("nothing", "Nothing downstream yet")]
        for s in stats:
            key = "won" if s.won else "leads" if s.leads else "contacted" if s.contacted else "accepted" if s.accepted else "nothing"
            next(g for g in buckets if g.key == key).keywords.append(s)
        return [g for g in buckets if g.keywords]
    if view == "table":
        return [Group("all", "All keywords", keywords=list(stats))]
    by_tier = {t: Group(t, TIER_LABELS[t], TIER_HELP[t]) for t in TIER_ORDER}
    for s in stats:
        by_tier[s.tier].keywords.append(s)
    return [g for g in by_tier.values() if g.keywords]


def tier_counts(stats: list[KeywordStat]) -> dict[str, int]:
    counts = dict.fromkeys(TIER_ORDER, 0)
    for s in stats:
        counts[s.tier] += 1
    return counts


__all__ = ["Group", "KeywordStat", "OfferingStat", "by_offering", "offering_stats", "SORTS", "TIER_LABELS", "TIER_ORDER", "VIEWS", "filtered", "grouped", "keyword_stats",
           "tier_counts", "tier_of"]

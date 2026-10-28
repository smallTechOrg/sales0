"""Assemble the dashboard: weekly table (computed + sheet + manual), secondary vs targets, funnel, yields, cost.

Merge rule (the one place it lives; shown in the UI): a cell is NEVER a sum of sources, so nothing is
double counted. Modes for the computed rows:

* ``max`` (default) - both computed and sheet present: the greater wins (the app only sees work done in
  the app; during the transition the team also worked in the sheet, so the larger figure is the better
  lower bound). Tie -> computed. Only one present -> that one.
* ``computed`` - the app's figure wins; the sheet fills only weeks the app has no data for.
* ``sheet`` - the sheet wins; computed fills only weeks the sheet has no figure for.

Weeks before the app's first recorded activity have no computed figure at all (shown as sheet or "-").
Manual rows (posts published, WhatsApp adds): a manual entry wins over the sheet in every mode except
``sheet``, where the sheet wins and the manual entry fills gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, utcnow
from sales0.config import get_settings
from sales0.db.models import ChannelGroup, Enquiry, EnquiryKeywordHit, LlmCall, Lead, Prospect, SeenPost
from sales0.logging import get_logger
from sales0.services import keyword_yield as ky
from sales0.services.metrics import compute, store
from sales0.services.metrics.weeks import Bucketer, Week, last_weeks, parse_week_key, week_containing, week_for_date, weeks_ending

log = get_logger("sales0.metrics")

MODES = ("max", "computed", "sheet")
MODE_LABELS = {"max": "Greater of app and sheet", "computed": "App first", "sheet": "Sheet first"}
WEEK_CHOICES = (4, 8, 12, 26, 52)
MAX_WEEKS = 104
DEFAULT_WEEKS = 12
PRUNE_MIN_SCANNED = 30  # a keyword scanned this many posts with zero relevant results is a prune candidate
PRUNE_MIN_RUNS = 2
SECONDARY_HISTORY = 5


@dataclass(frozen=True)
class Cell:
    value: float | None
    source: str  # computed | sheet | manual | none
    computed: float | None = None
    sheet: float | None = None
    manual: float | None = None

    @property
    def shown(self) -> str:
        return "-" if self.value is None else (str(int(self.value)) if float(self.value).is_integer() else f"{self.value:g}")


@dataclass
class WeeklyRow:
    key: str
    label: str
    kind: str
    cells: list[Cell]

    @property
    def total(self) -> float:
        return sum(c.value or 0 for c in self.cells)


@dataclass
class SourceRow:
    key: str
    label: str
    enabled: bool
    note: str
    actual: Cell | None
    target: float | None
    target_origin: str  # override | sheet | default | none
    pct: float | None
    prev: Cell | None
    delta: float | None
    history: list[tuple[str, Cell]]


@dataclass(frozen=True)
class FunnelStage:
    key: str
    label: str
    count: int
    pct_prev: float | None
    pct_first: float | None


@dataclass
class Funnel:
    stages: list[FunnelStage]
    weekly: list[dict[str, int]]  # per week, stage key -> count (cohort = week the post became an enquiry)


@dataclass
class KeywordRow:
    keyword_id: int | None
    text: str
    active: bool
    scanned: int
    relevant: int
    relevant_pct: float
    prospects: int
    runs: int
    cost_micros: int
    cost_per_relevant_micros: int | None
    prune: bool


@dataclass
class GroupRow:
    name: str
    platform: str
    active: bool
    scanned: int | None
    relevant: int | None
    note: str


@dataclass
class CostSummary:
    total_micros: int
    relevant: int
    per_relevant_micros: int | None
    weekly_micros: list[int]
    weekly_per_relevant: list[int | None]


@dataclass
class Dashboard:
    now: datetime
    tz: ZoneInfo
    weeks: list[Week]
    mode: str
    app_start: Week | None
    weekly: list[WeeklyRow]
    selected: Week
    secondary: list[SourceRow]
    funnel: Funnel
    keywords: list[KeywordRow]
    groups: list[GroupRow]
    cost: CostSummary
    scanned_weekly: list[int] = field(default_factory=list)
    empty: bool = True


# --------------------------------------------------------------------------- merge
def resolve(mode: str, kind: str, computed: float | None, sheet: float | None, manual: float | None = None) -> Cell:
    def cell(v: float | None, src: str) -> Cell:
        return Cell(v, src if v is not None else "none", computed, sheet, manual)

    if kind == "manual":
        order = [(sheet, "sheet"), (manual, "manual")] if mode == "sheet" else [(manual, "manual"), (sheet, "sheet")]
        for v, src in order:
            if v is not None:
                return cell(v, src)
        return cell(None, "none")
    if computed is not None and sheet is not None:
        if mode == "max":
            return cell(computed, "computed") if computed >= sheet else cell(sheet, "sheet")
        return cell(sheet, "sheet") if mode == "sheet" else cell(computed, "computed")
    if computed is not None:
        return cell(computed, "computed")
    return cell(sheet, "sheet")


def parse_mode(value: str | None) -> str:
    return value if value in MODES else "max"


def parse_weeks(value: str | int | None) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_WEEKS
    return n if 1 <= n <= MAX_WEEKS else DEFAULT_WEEKS


# --------------------------------------------------------------------------- funnel
def _funnel(db: Session, weeks: list[Week], scanned: list[int]) -> Funnel:
    b = Bucketer(weeks)
    rows = db.execute(
        select(
            Enquiry.id, Enquiry.date_added, Enquiry.review_status, Prospect.id, Prospect.contacted_at,
            Prospect.responded_at, Lead.id, Lead.stage, Lead.proposal_sent_on,
        )
        .select_from(Enquiry)
        .outerjoin(Prospect, Prospect.id == Enquiry.prospect_id)
        .outerjoin(Lead, Lead.prospect_id == Prospect.id)
        .where(compute.relevant_clause(), Enquiry.date_added >= b.lo, Enquiry.date_added < b.hi)
    ).all()
    per: dict[int, dict] = {}
    for eid, added, review, pid, contacted, responded, lid, lstage, proposal_on in rows:
        f = per.setdefault(eid, {"week": b.index(added), "accepted": False, "contacted": False, "replied": False, "proposal": False, "won": False})
        f["accepted"] |= review == "accepted" or pid is not None
        f["contacted"] |= contacted is not None
        f["replied"] |= responded is not None or lid is not None
        f["proposal"] |= lid is not None and (proposal_on is not None or lstage in ("proposal_sent", "won"))
        f["won"] |= lstage == "won"
    keys = ("accepted", "contacted", "replied", "proposal", "won")
    weekly = [dict.fromkeys(("scanned", "relevant", *keys), 0) for _ in weeks]
    for i, s in enumerate(scanned):
        weekly[i]["scanned"] = s
    for f in per.values():
        i = f["week"]
        if i is None:
            continue
        # "reached at least this stage": a later stage implies the earlier ones
        won = f["won"]
        proposal = f["proposal"] or won
        replied = f["replied"] or proposal
        contacted = f["contacted"] or replied
        accepted = f["accepted"] or contacted
        weekly[i]["relevant"] += 1
        for k, v in zip(keys, (accepted, contacted, replied, proposal, won), strict=True):
            weekly[i][k] += int(v)
    labels = (("scanned", "Scanned"), ("relevant", "Relevant"), ("accepted", "Accepted prospect"), ("contacted", "Contacted"),
              ("replied", "Replied / lead"), ("proposal", "Proposal"), ("won", "Won / client"))
    totals = {k: sum(w[k] for w in weekly) for k, _ in labels}
    stages: list[FunnelStage] = []
    prev: int | None = None
    for k, label in labels:
        n = totals[k]
        pct_prev = round(100 * n / prev, 1) if prev else None
        pct_first = round(100 * n / totals["scanned"], 2) if totals["scanned"] else None
        stages.append(FunnelStage(k, label, n, pct_prev, pct_first))
        prev = n
    return Funnel(stages, weekly)


# --------------------------------------------------------------------------- yields + cost
def _keywords(db: Session, since: datetime) -> list[KeywordRow]:
    base = ky.keyword_yield(db, since=since)
    prospects: dict[str, set[int]] = {}
    for text, eid in db.execute(
        select(EnquiryKeywordHit.keyword_text, EnquiryKeywordHit.enquiry_id)
        .join(Enquiry, Enquiry.id == EnquiryKeywordHit.enquiry_id)
        .where(Enquiry.ai_verdict == "genuine", Enquiry.prospect_id.is_not(None), EnquiryKeywordHit.seen_at >= since)
    ):
        prospects.setdefault(text.strip().lower(), set()).add(eid)
    cost: dict[str, int] = {}
    for text, total in db.execute(
        select(SeenPost.keyword_text, func.coalesce(func.sum(LlmCall.cost_usd_micros), 0))
        .join(SeenPost, SeenPost.id == LlmCall.seen_post_id)
        .where(LlmCall.created_at >= since)
        .group_by(SeenPost.keyword_text)
    ):
        cost[text.strip().lower()] = cost.get(text.strip().lower(), 0) + int(total or 0)
    out = []
    for y in base:
        k = y.text.strip().lower()
        c = cost.get(k, 0)
        out.append(KeywordRow(
            y.keyword_id, y.text, y.active, y.scanned, y.relevant, y.relevant_pct, len(prospects.get(k, ())), y.runs, c,
            round(c / y.relevant) if y.relevant else None,
            y.active and y.relevant == 0 and y.scanned >= PRUNE_MIN_SCANNED and y.runs >= PRUNE_MIN_RUNS,
        ))
    return out


def _groups(db: Session) -> list[GroupRow]:
    return [
        GroupRow(g.name, g.platform, g.active, None, None,
                 "not enabled" if g.platform == "whatsapp" else "no scan data (LinkedIn groups are not scraped)")
        for g in db.scalars(select(ChannelGroup).where(ChannelGroup.sheet_removed_at.is_(None)).order_by(ChannelGroup.name))
    ]


def _cost(db: Session, weeks: list[Week], relevant_weekly: list[int]) -> CostSummary:
    b = Bucketer(weeks)
    weekly = [0] * len(weeks)
    for created, micros in db.execute(
        select(LlmCall.created_at, LlmCall.cost_usd_micros).where(LlmCall.created_at >= b.lo, LlmCall.created_at < b.hi)
    ):
        i = b.index(created)
        if i is not None:
            weekly[i] += int(micros or 0)
    total, rel = sum(weekly), sum(relevant_weekly)
    return CostSummary(
        total, rel, round(total / rel) if rel else None, weekly,
        [round(c / r) if r else None for c, r in zip(weekly, relevant_weekly, strict=True)],
    )


# --------------------------------------------------------------------------- dashboard
def build_dashboard(
    db: Session, *, now: datetime | None = None, n_weeks: int = DEFAULT_WEEKS, mode: str = "max", week_key: str | None = None
) -> Dashboard:
    tz = get_settings().tz
    now = ensure_utc(now or utcnow())
    mode = parse_mode(mode)
    cur = week_containing(now, tz)
    weeks = last_weeks(now, tz, n_weeks)
    selected = parse_week_key(week_key, tz) if week_key else None
    selected = selected if selected is not None and selected.monday <= cur.monday else cur

    # compute over the union of the displayed weeks and the secondary window
    sec_weeks = weeks_ending(selected, tz, SECONDARY_HISTORY)
    span_first = min(weeks[0].monday, sec_weeks[0].monday)
    span_last = max(weeks[-1].monday, sec_weeks[-1].monday)
    all_weeks = []
    w = week_for_date(span_first, tz)
    while w.monday <= span_last:
        all_weeks.append(w)
        w = week_for_date(w.monday.fromordinal(w.monday.toordinal() + 7), tz)
    counters = compute.compute_counters(db, all_weeks)
    index = {wk.key: i for i, wk in enumerate(all_weeks)}

    first = compute.app_first_activity(db)
    app_start = week_for_date(ensure_utc(first).astimezone(tz).date(), tz) if first else None

    def computed_value(counter: str, wk: Week) -> float | None:
        if app_start is None or wk.monday < app_start.monday:
            return None
        return float(counters[counter][index[wk.key]])

    base = store.sheet_baseline(db, ensure_utc(now).astimezone(tz).date())
    manual = store.manual_values(db)

    counter_of = {
        "enquiries_added": "enquiries_added", "enquiries_contacted": "enquiries_contacted",
        "meetings_proposals": "meetings_proposals", "old_prospects": "old_prospects",
    }
    weekly_rows: list[WeeklyRow] = []
    for key, label, kind in store.WEEKLY_ROWS:
        cells = []
        for wk in weeks:
            c = computed_value(counter_of[key], wk) if kind == "computed" else None
            s = base.weekly.get((key, wk.iso_year, wk.iso_week))
            m = manual.get((key, wk.iso_year, wk.iso_week)) if kind == "manual" else None
            cells.append(resolve(mode, kind, c, s, None if m is None else float(m)))
        weekly_rows.append(WeeklyRow(key, label, kind, cells))

    overrides = store.target_overrides(db)
    secondary: list[SourceRow] = []
    for src in store.SOURCES:
        def cell_for(wk: Week, src=src) -> Cell | None:
            if not src.enabled:
                return None
            c = computed_value(src.counter, wk) if src.counter else None
            s = base.secondary.get((src.key, wk.iso_year, wk.iso_week))
            return resolve(mode, "computed", c, s)

        if src.key in overrides:
            target, origin = overrides[src.key], "override"
        elif src.key in base.targets:
            target, origin = base.targets[src.key], "sheet"
        elif src.default_target is not None:
            target, origin = float(src.default_target), "default"
        else:
            target, origin = None, "none"
        if not src.enabled:
            target = None
        hist = [(wk.label, cell_for(wk)) for wk in sec_weeks]
        actual, prev = (hist[-1][1], hist[-2][1]) if src.enabled else (None, None)
        pct = (round(100 * (actual.value or 0) / target, 1) if actual and target else None)
        delta = (actual.value - prev.value) if actual and prev and actual.value is not None and prev.value is not None else None
        secondary.append(SourceRow(
            src.key, src.label, src.enabled, src.note, actual, target, origin, pct, prev, delta,
            [(lbl, c) for lbl, c in hist if c is not None],
        ))

    wi = [index[wk.key] for wk in weeks]
    scanned_weekly = [counters["scanned"][i] for i in wi]
    relevant_weekly = [counters["enquiries_added"][i] for i in wi]
    funnel = _funnel(db, weeks, scanned_weekly)
    try:
        with db.begin_nested():
            keywords = _keywords(db, weeks[0].start_utc)
    except SQLAlchemyError:
        log.warning("metrics_keywords_unavailable", exc_info=True)
        keywords = []
    cost = _cost(db, weeks, relevant_weekly)
    empty = not any(scanned_weekly) and not any(relevant_weekly) and app_start is None and not base.weekly
    return Dashboard(
        now, tz, weeks, mode, app_start, weekly_rows, selected, secondary, funnel, keywords, _groups(db), cost,
        scanned_weekly, empty,
    )


__all__ = [
    "DEFAULT_WEEKS", "MODES", "MODE_LABELS", "WEEK_CHOICES", "Cell", "Dashboard", "build_dashboard",
    "parse_mode", "parse_weeks", "resolve",
]

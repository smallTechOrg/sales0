"""CSV export of each dashboard table. Cells beginning with = + - @ are prefixed with a quote so a
spreadsheet never evaluates untrusted text (keyword text comes from LinkedIn/user input)."""

from __future__ import annotations

import csv
import io

from sales0.services.metrics.report import Dashboard

TABLES = ("weekly", "secondary", "funnel", "keywords", "groups", "cost")


def _safe(v: object) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "@") or (s[:1] == "-" and not _is_number(s)) else s


def _is_number(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


def _usd(micros: int | None) -> str:
    return "" if micros is None else f"{micros / 1_000_000:.4f}"


def table(d: Dashboard, name: str) -> tuple[list[str], list[list[object]]]:
    if name == "weekly":
        head = ["Metric", *[w.key for w in d.weeks]]
        rows: list[list[object]] = []
        for r in d.weekly:
            rows.append([r.label, *[c.shown if c.value is not None else "" for c in r.cells]])
            rows.append([f"{r.label} (source)", *[c.source for c in r.cells]])
        return head, rows
    if name == "secondary":
        head = ["Source", "Week", "Actual", "Target", "Progress %", "Previous week", "Change", "Actual from", "Target from"]
        rows = [
            [s.label, d.selected.key, s.actual.shown if s.actual else "not enabled", "" if s.target is None else f"{s.target:g}",
             "" if s.pct is None else s.pct, s.prev.shown if s.prev else "", "" if s.delta is None else f"{s.delta:g}",
             s.actual.source if s.actual else "", s.target_origin]
            for s in d.secondary
        ]
        return head, rows
    if name == "funnel":
        head = ["Stage", "Count", "% of previous stage", "% of scanned"]
        return head, [[s.label, s.count, "" if s.pct_prev is None else s.pct_prev, "" if s.pct_first is None else s.pct_first] for s in d.funnel.stages]
    if name == "keywords":
        head = ["Keyword", "Active", "Scanned", "Relevant", "Relevant %", "Prospects", "Runs", "Cost USD", "Cost per relevant USD", "Prune candidate"]
        return head, [
            [k.text, "yes" if k.active else "no", k.scanned, k.relevant, k.relevant_pct, k.prospects, k.runs, _usd(k.cost_micros),
             _usd(k.cost_per_relevant_micros), "yes" if k.prune else ""]
            for k in d.keywords
        ]
    if name == "groups":
        head = ["Group", "Platform", "Active", "Scanned", "Relevant", "Note"]
        return head, [[g.name, g.platform, "yes" if g.active else "no", "" if g.scanned is None else g.scanned,
                       "" if g.relevant is None else g.relevant, g.note] for g in d.groups]
    if name == "cost":
        head = ["Week", "AI cost USD", "Relevant enquiries", "Cost per relevant USD"]
        rel = [w["relevant"] for w in d.funnel.weekly]
        return head, [[w.key, _usd(c), r, _usd(p)] for w, c, r, p in zip(d.weeks, d.cost.weekly_micros, rel, d.cost.weekly_per_relevant, strict=True)]
    raise KeyError(name)


def to_csv(d: Dashboard, name: str) -> str:
    head, rows = table(d, name)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([_safe(h) for h in head])
    for r in rows:
        w.writerow([_safe(c) for c in r])
    return buf.getvalue()


__all__ = ["TABLES", "table", "to_csv"]

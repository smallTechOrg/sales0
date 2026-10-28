"""Digest section "Weekly pulse": this ISO week so far against the weekly targets.

Registered with ``digest.register_section`` (idempotent). "This week" is the local ISO week containing
the digest window's end, so a rebuilt past digest is deterministic.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from sales0.services import digest
from sales0.services.digest import DigestSection, SectionResult, Window
from sales0.services.metrics import report


def _pulse(db: Session, w: Window) -> SectionResult:
    dash = report.build_dashboard(db, now=w.end, n_weeks=4)
    items: list[digest.Item] = []
    behind = 0
    for row in dash.secondary:
        if not row.enabled or row.actual is None or row.target is None:
            continue
        got = row.actual.value or 0
        if row.pct is not None and row.pct < 100:
            behind += 1
        items.append({"text": f"{row.label}: {got:g} of {row.target:g} ({row.pct:g}%)", "href": "/metrics"})
    added = dash.weekly[0].cells[-1].shown
    stats = {"week": dash.selected.key, "sources_behind_target": behind, "enquiries_added": added}
    return SectionResult(items, stats, "No weekly targets set.")


SECTION = DigestSection("weekly_pulse", "Weekly pulse", 28, _pulse)


def register() -> None:
    digest.register_section(SECTION, replace=True)


__all__ = ["SECTION", "register"]

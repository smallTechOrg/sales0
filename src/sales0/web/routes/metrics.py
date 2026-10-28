"""``/metrics`` - the Weekly + Secondary dashboard, funnel, keyword/group yield, cost per relevant
enquiry, manual weekly figures, target overrides and CSV export of every table (read-mostly; the two
POSTs write only ``manual_metrics`` and the ``metrics_target.*`` app_meta keys)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from sales0.db.engine import get_db
from sales0.errors import DomainError
from sales0.services.metrics import charts, exports, report, store
from sales0.web.acting import clear_flash, get_acting_member_optional, pop_flash, set_flash
from sales0.web.context import render_page
from sales0.web.envelope import wants_json

router = APIRouter()

SORT_KEYS = {
    "text": lambda k: k.text.lower(), "scanned": lambda k: k.scanned, "relevant": lambda k: k.relevant,
    "pct": lambda k: k.relevant_pct, "prospects": lambda k: k.prospects, "cost": lambda k: k.cost_micros,
    "per_relevant": lambda k: k.cost_per_relevant_micros if k.cost_per_relevant_micros is not None else -1,
}


def _query(weeks: int, mode: str, week: str | None) -> str:
    q = f"weeks={weeks}&mode={mode}"
    return q + (f"&week={week}" if week else "")


def _actor(member) -> str:  # noqa: ANN001
    return (getattr(member, "email", None) or "team") if member is not None else "team"


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(
    request: Request, db: Session = Depends(get_db), weeks: str | None = None, mode: str | None = None,
    week: str | None = None, sort: str = "scanned", dir: str = "desc",  # noqa: A002
) -> HTMLResponse:
    n, m = report.parse_weeks(weeks), report.parse_mode(mode)
    dash = report.build_dashboard(db, n_weeks=n, mode=m, week_key=week)
    key = SORT_KEYS.get(sort, SORT_KEYS["scanned"])
    desc = dir != "asc"
    keywords = sorted(dash.keywords, key=key, reverse=desc)
    labels = [w.label for w in dash.weeks]
    by_key = {r.key: r for r in dash.weekly}

    def vals(row_key: str) -> list[float | None]:
        return [c.value for c in by_key[row_key].cells]

    charts_html = {
        "weekly": charts.bar_chart("Enquiries added and contacted per week", labels,
                                   [("Enquiries added", vals("enquiries_added")), ("Contacted", vals("enquiries_contacted"))]),
        "trend": charts.line_chart("Scanned posts vs relevant enquiries", labels,
                                   [("Scanned", [float(x) for x in dash.scanned_weekly]),
                                    ("Relevant", [float(w["relevant"]) for w in dash.funnel.weekly])]),
        "funnel": charts.funnel_chart("Funnel: scanned to won", [(s.label, s.count, s.pct_prev) for s in dash.funnel.stages]),
        "cost": charts.line_chart("AI cost per relevant enquiry (USD)", labels,
                                  [("USD", [None if c is None else round(c / 1_000_000, 4) for c in dash.cost.weekly_per_relevant])]),
    }
    flash = pop_flash(request)
    response = render_page(
        request, db, "metrics/page.html",
        {
            "d": dash, "weeks_n": n, "mode": m, "week_choices": report.WEEK_CHOICES, "modes": report.MODE_LABELS,
            "keywords": keywords, "sort": sort, "dir": "desc" if desc else "asc", "charts": charts_html, "flash": flash,
            "q": _query(n, m, week), "manual_keys": [r for r in store.WEEKLY_ROWS if r[2] == "manual"],
            "tables": exports.TABLES, "prune_min_scanned": report.PRUNE_MIN_SCANNED, "prune_min_runs": report.PRUNE_MIN_RUNS, "week_param": week,
            "usd": lambda micros: "-" if micros is None else f"${micros / 1_000_000:.4f}",
        },
    )
    if flash:
        clear_flash(response)
    return response


@router.get("/metrics/export/{name}.csv")
def metrics_csv(
    name: str, db: Session = Depends(get_db), weeks: str | None = None, mode: str | None = None, week: str | None = None
) -> Response:
    if name not in exports.TABLES:
        raise DomainError("not_found", "No such table.", 404)
    dash = report.build_dashboard(db, n_weeks=report.parse_weeks(weeks), mode=report.parse_mode(mode), week_key=week)
    return Response(
        exports.to_csv(dash, name), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="sales0-metrics-{name}.csv"'},
    )


def _back(request: Request, back: str, message: str, kind: str = "ok") -> Response:
    target = back if back.startswith("/metrics") else "/metrics"
    response = RedirectResponse(target, status_code=303)
    set_flash(response, message, kind)
    return response


def _int_or_none(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise DomainError("value_invalid", "Enter a whole number.", 400) from None


@router.post("/metrics/manual")
def metrics_manual(
    request: Request, db: Session = Depends(get_db), member=Depends(get_acting_member_optional),
    metric: str = Form(...), week: str = Form(...), value: str = Form(""), back: str = Form("/metrics"),
) -> Response:
    try:
        try:
            year_s, week_s = week.upper().split("-W")
            store.set_manual(db, metric, int(year_s), int(week_s), _int_or_none(value), actor=_actor(member))
        except ValueError:
            raise DomainError("week_invalid", "That is not a valid ISO week.", 400) from None
    except DomainError as exc:
        if wants_json(request):
            raise
        return _back(request, back, exc.message, "error")
    return _back(request, back, f"Saved {week} figure.")


@router.post("/metrics/targets")
def metrics_target(
    request: Request, db: Session = Depends(get_db), source: str = Form(...), value: str = Form(""), back: str = Form("/metrics"),
) -> Response:
    try:
        try:
            number = float(value) if value.strip() else None
        except ValueError:
            raise DomainError("value_invalid", "Enter a number.", 400) from None
        store.set_target_override(db, source, number)
    except DomainError as exc:
        if wants_json(request):
            raise
        return _back(request, back, exc.message, "error")
    return _back(request, back, "Target updated." if value.strip() else "Target override removed; the sheet value applies again.")

"""``/costs`` — AI spend: daily/monthly totals, per feature and model, cost per enquiry/draft,
and the monthly spend-cap status (read from ``llm_calls`` only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.services import costs_report
from sales0.web.context import render_page

router = APIRouter()


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    report = costs_report.build_report(db)
    max_day = max((b.cost_micros for b in report.daily), default=0) or 1
    return render_page(
        request, db, "costs/page.html",
        {"report": report, "max_day": max_day, "cap_usd": get_settings().monthly_spend_cap_usd},
    )

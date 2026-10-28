"""Shell context (header light, alert banner, cost badge, nav) + shared render helpers.

Every page template extends ``base.html`` and receives ``shell`` from :func:`shell_context`.
Routes render through :func:`render_page` (full page) or :func:`render_fragment` (htmx).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sales0.clock import ensure_utc, local_today, utcnow
from sales0.config import get_settings
from sales0.services import costs as costs_service
from sales0.services import costs_report
from sales0.services import runs as runs_service
from sales0.services import session as session_service
from sales0.services.team import acting as acting_service
from sales0.services.team import members as members_service
from sales0.services.views import SessionStatusView
from sales0.web.envelope import is_htmx
from sales0.web.templating import ago, get_templates


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    phase: int | None
    active: bool = False


# (label, href, phase) — phase None = real in Phase 1; otherwise the phase that builds it.
NAV: tuple[tuple[str, str, int | None], ...] = (
    ("Inbox", "/inbox", None),
    ("Runs", "/runs", None),
    ("Setup", "/setup", None),
    ("Pipeline", "/pipeline", 2),
    ("Contacts", "/contacts", 2),
    ("Competitors & Groups", "/competitors", 2),
    ("Import", "/import", 2),
    ("Outreach", "/outreach", None),
    ("Metrics", "/metrics", 4),
    ("Digest", "/digest", None),
    ("Suggestions", "/suggestions", None),
    ("Costs", "/costs", None),
    ("Schedule", "/schedule", None),
    ("WhatsApp", "/whatsapp", 5),
)

OPPORTUNITY_LABELS = {
    "direct_buyer": "Direct buyer",
    "agency_requirement": "Agency requirement",
    "individual_trainer_wanted": "Trainer wanted",
}

POSTER_TYPE_LABELS = {
    "buyer_company": "Buyer company",
    "training_agency": "Training agency",
    "individual_trainer_seeker": "Seeks individual trainer",
    "job_seeker_or_self_promo": "Job seeker / self-promo",
    "other": "Other",
}


def nav_items(path: str) -> list[NavItem]:
    items = []
    for label, href, phase in NAV:
        active = path == href or path.startswith(href + "/")
        items.append(NavItem(label=label, href=href, phase=phase, active=active))
    return items


def banner_text(session: SessionStatusView) -> str | None:
    if session.status != "needs_attention":
        return None
    reason = (session.last_error or "the last LinkedIn check found a problem").rstrip(". ")
    return f"LinkedIn needs attention — {reason}. Runs are paused to protect your account."


def status_light_view(session: SessionStatusView) -> dict[str, Any]:
    """Modifier class + one-line label for the header light."""
    if session.status == "busy":
        label = {"connect": "Connecting…", "check": "Checking…", "run": "Run in progress"}.get(
            session.busy_action or "", "Working…"
        )
        return {"modifier": "busy", "label": label, "busy": True}
    if session.status == "connected":
        if session.last_successful_run_at is not None:
            label = f"LinkedIn connected · last run {ago_text(session.last_successful_run_at)}"
        else:
            label = "LinkedIn connected · no runs yet"
        return {"modifier": "connected", "label": label, "busy": False}
    if session.status == "needs_attention":
        return {"modifier": "needs-attention", "label": "LinkedIn needs attention", "busy": False}
    return {"modifier": "not-connected", "label": "LinkedIn not connected", "busy": False}


def shell_context(request: Request, db: Session) -> dict[str, Any]:
    session = session_service.session_status(db)
    acting_member = acting_service.resolve_acting_member(db, request.cookies.get(acting_service.COOKIE_NAME))
    return {
        "acting_member": acting_member,
        "team_members": members_service.list_members(db, include_inactive=False),
        "session": session,
        "cost": costs_service.cost_summary(db),
        "active_run": runs_service.active_run(db),
        "nav": nav_items(request.url.path),
        "banner": banner_text(session),
        "spend_cap": costs_report.spend_cap_status(db),
        "mode": get_settings().linkedin_mode,
        "light": status_light_view(session),
    }


# ---------------------------------------------------------------------- display helpers
def ago_text(value: datetime | None) -> str:
    return ago(ensure_utc(value) if value else None)


def date_ago(value: date | datetime | None) -> str:
    """'today' / '1d ago' / '3w ago' for a posted date (the scraper estimates whole days)."""
    if value is None:
        return "date unknown"
    if isinstance(value, datetime):
        return ago_text(value)
    days = (local_today(get_settings().tz) - value).days
    if days <= 0:
        return "today"
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    return value.strftime("%d %b %Y")


def tokens_short(count: int | None) -> str:
    if not count:
        return "0 tok"
    if count >= 1000:
        return f"{count / 1000:.1f}k tok"
    return f"{count} tok"


def duration_text(ms: int | None) -> str:
    if ms is None:
        return "—"
    seconds = int(round(ms / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def elapsed_ms(started_at: datetime | None, finished_at: datetime | None = None) -> int | None:
    if started_at is None:
        return None
    end = ensure_utc(finished_at) if finished_at else utcnow()
    return max(0, int((end - ensure_utc(started_at)).total_seconds() * 1000))


def clock_text(seconds: int | None) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def cooldown_clock(until: datetime | None, now: datetime | None = None) -> str:
    """The safety cool-down instant as local ``HH:MM`` (rounded up; "HH:MM tomorrow" on a
    later local day). Delegates to the service so the Run-now reason and the page lines
    always print the same time."""
    if until is None:
        return ""
    return runs_service.cooldown_clock(until, now)


def browser_is_chrome(session: SessionStatusView) -> bool:
    """True when Connect opens Google Chrome (copy says "Chrome window"), False for the
    built-in-browser fallback (copy says "browser window")."""
    label = getattr(session, "browser_label", "") or ""
    return label.startswith("Google Chrome") and not getattr(session, "browser_note", None)


def usd_precise(micros: int | None) -> str:
    """Cost totals: 4 decimals below $1 (ui.md "AI today $0.0123 · month $0.4567"), else 2."""
    if micros is None:
        return "—"
    value = micros / 1_000_000
    if micros == 0:
        return "$0.00"
    if abs(value) < 1:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def opportunity_label(opportunity_type: str | None) -> str | None:
    """Chip text for the inbox card; ``other`` / unknown values show no chip."""
    return OPPORTUNITY_LABELS.get(opportunity_type or "")


def poster_label(poster_type: str | None) -> str | None:
    if not poster_type:
        return None
    return POSTER_TYPE_LABELS.get(poster_type, poster_type.replace("_", " ").capitalize())


def pct_text(part: int | None, whole: int | None) -> str:
    """'1.5%' for part/whole (one decimal below 10%, whole numbers above); '—' when whole is 0."""
    if not whole:
        return "—"
    value = (part or 0) * 100 / whole
    return f"{value:.1f}%" if value < 10 else f"{value:.0f}%"


def posted_hint(posted_relative: str | None) -> str | None:
    """'posted ~3h before sales0 found it' for the scraped relative age ('3h'); None if unknown."""
    value = (posted_relative or "").strip()
    return f"posted ~{value} before sales0 found it" if value else None


def interrupted(run: Any) -> bool:
    """A run a server restart cut short (Phase 1.4: aborted, no abort reason)."""
    return bool(run) and run.status == "aborted" and not run.abort_reason


HELPERS: dict[str, Any] = {
    "pct_text": pct_text,
    "posted_hint": posted_hint,
    "interrupted": interrupted,
    "date_ago": date_ago,
    "tokens_short": tokens_short,
    "duration_text": duration_text,
    "elapsed_ms": elapsed_ms,
    "clock_text": clock_text,
    "poster_label": poster_label,
    "opportunity_label": opportunity_label,
    "usd_precise": usd_precise,
    "ago_text": ago_text,
    "cooldown_clock": cooldown_clock,
    "browser_is_chrome": browser_is_chrome,
}


# ---------------------------------------------------------------------- render helpers
def render_page(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    shell = shell_context(request, db)
    ctx: dict[str, Any] = {**HELPERS, "shell": shell, "acting_member": shell["acting_member"]}
    ctx.update(context or {})
    return get_templates().TemplateResponse(request, template, ctx, status_code=status_code, headers=headers)


def render_fragment(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    ctx: dict[str, Any] = {**HELPERS}
    ctx.update(context or {})
    return get_templates().TemplateResponse(request, template, ctx, headers=headers)


def render_html(request: Request, template: str, context: dict[str, Any] | None = None) -> str:
    """Render a template to a string (used to concatenate a fragment with oob swaps)."""
    ctx: dict[str, Any] = {"request": request, **HELPERS}
    ctx.update(context or {})
    return get_templates().get_template(template).render(ctx)


def toast_html(message: str, kind: str = "ok") -> str:
    """An out-of-band toast appended to #toasts alongside a normal fragment."""
    return render_html_static("_shell/toast_oob.html", {"message": message, "kind": kind})


def render_html_static(template: str, context: dict[str, Any]) -> str:
    return get_templates().get_template(template).render({**HELPERS, **context})


def cost_badge_oob(db: Session) -> str:
    return render_html_static("_shell/cost_badge.html", {"cost": costs_service.cost_summary(db), "oob": True})


def redirect(path: str, **params: Any) -> RedirectResponse:
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{path}?{urlencode(clean)}" if clean else path
    return RedirectResponse(url, status_code=303)


__all__ = [
    "HELPERS",
    "NAV",
    "NavItem",
    "banner_text",
    "browser_is_chrome",
    "cooldown_clock",
    "cost_badge_oob",
    "is_htmx",
    "nav_items",
    "redirect",
    "render_fragment",
    "render_html",
    "render_page",
    "shell_context",
    "status_light_view",
    "toast_html",
]

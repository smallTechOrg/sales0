"""Setup page (slice F): LinkedIn connection card, keywords, offerings, safety limits."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.engine import get_db
from sales0.db.models import TeamMember
from sales0.errors import DomainError
from sales0.jobs.runner import get_runner
from sales0.services import keywords as keywords_service
from sales0.services import linkedin_accounts as accounts_service
from sales0.services import offerings as offerings_service
from sales0.services import session as session_service
from sales0.services.views import OfferingInput, OfferingView, SessionStatusView
from sales0.web.acting import get_acting_member_optional
from sales0.web.context import PathId
from sales0.web.context import is_htmx, redirect, render_html, render_page, toast_html
from sales0.web.envelope import ok

router = APIRouter()

OFFERING_INVALID = "Offering name (1–80) and group (1–60) are required."


# ---------------------------------------------------------------------- helpers
def _status_key(session: SessionStatusView) -> str:
    return f"{session.status}:{session.busy_action or ''}"


def _safety_caps(session: SessionStatusView) -> list[tuple[str, str]]:
    s = get_settings()
    return [
        ("Keywords per run", f"{s.scrape_max_keywords_per_run}"),
        ("Posts per keyword", f"{s.scrape_max_posts_per_keyword}"),
        ("Posts scanned per day", f"{s.scrape_max_posts_per_day}"),
        ("Runs per day", f"{s.scrape_max_runs_per_day}"),
        ("Delay between page actions", f"{s.scrape_delay_min_s:g}–{s.scrape_delay_max_s:g} s (random)"),
        ("Pause between keywords", f"{s.scrape_keyword_pause_min_s:g}–{s.scrape_keyword_pause_max_s:g} s (random)"),
        ("Scrolls per keyword", f"{s.scrape_max_scrolls}"),
        ("Search order", "Latest first" if s.scrape_sort == "latest" else "Most relevant first"),
        ("Post date filters (one pass each)", ", ".join(s.scrape_date_filters)),
        ("Posts triaged at once", f"{s.triage_concurrency}"),
        ("Connect waits for login", f"{s.linkedin_connect_timeout_s} s"),
        ("Browser", session.browser_label),
        ("Runs", "hidden (headless)"),
        ("Cool-down after rate limit / captcha", f"{s.safety_cooldown_min} min"),
        ("Cool-down after checkpoint / logout", f"{s.safety_cooldown_checkpoint_min} min"),
    ]


def _offerings_ctx(db: Session) -> dict:
    """Active offerings grouped by group name, keeping the service's order (groups shown once)."""
    groups: dict[str, list[OfferingView]] = {}
    for offering in offerings_service.list_offerings(db):
        groups.setdefault(offering.group_name, []).append(offering)
    return {
        "offering_groups": list(groups.items()),
        "group_names": offerings_service.list_group_names(db),
    }


def _find_offering(db: Session, offering_id: PathId) -> OfferingView:
    for offering in offerings_service.list_offerings(db, include_inactive=True):
        if offering.id == offering_id:
            return offering
    raise DomainError("not_found", "That item no longer exists — refresh the page.", 404)


def _offering_input(name: str, group_name: str, description: str) -> OfferingInput:
    try:
        return OfferingInput(name=name.strip(), group_name=group_name.strip(), description=description.strip())
    except ValidationError as exc:
        raise DomainError("offering_invalid", OFFERING_INVALID, 400) from exc


def _html(*parts: str, headers: dict[str, str] | None = None) -> HTMLResponse:
    return HTMLResponse("".join(parts), headers=headers)


def _linkedin_card(request: Request, session: SessionStatusView, *, changed: bool) -> HTMLResponse:
    html = render_html(request, "setup/_linkedin_card.html", {"session": session})
    return _html(html, headers={"HX-Trigger": "session-changed"} if changed else None)


def _accounts_ctx(db: Session, *, notice: str | None = None, error: str | None = None) -> dict:
    return {
        "accounts": accounts_service.list_accounts(db),
        "account_busy": get_runner().busy(),
        "accounts_notice": notice,
        "accounts_error": error,
        "fixture_mode": get_settings().linkedin_mode == "fixture",
    }


def _accounts_card(
    request: Request, db: Session, *, notice: str | None = None, error: str | None = None, changed: bool = True
) -> HTMLResponse:
    """The card (+ a toast). ``changed`` sends ``session-changed`` (header light, main card); the refresh GET
    must not, or the card's own ``session-changed`` listener would loop."""
    html = render_html(request, "setup/_linkedin_accounts.html", _accounts_ctx(db, error=error))
    return _html(html, toast_html(notice) if notice else "", headers={"HX-Trigger": "session-changed"} if changed else None)


OWN_GROUP = "Your keywords"


def _keywords_ctx(db: Session) -> dict:
    """Keywords grouped for the Setup card: one group per offering name (suggested phrases),
    then any other group, then "Your keywords" (no group). Empty groups are not shown."""
    rows = keywords_service.list_keywords(db)
    by_group: dict[str, list] = {}
    for keyword in rows:
        by_group.setdefault(keyword.group_name or OWN_GROUP, []).append(keyword)
    offering_names = [o.name for o in offerings_service.list_offerings(db)]
    order = [n for n in offering_names if n in by_group]
    order += [n for n in by_group if n not in order and n != OWN_GROUP]
    if OWN_GROUP in by_group:
        order.append(OWN_GROUP)
    groups = [
        {
            "name": name,
            "keywords": by_group[name],
            "scanned": sum(k.scanned_total for k in by_group[name]),
            "relevant": sum(k.relevant_total for k in by_group[name]),
        }
        for name in order
    ]
    group_choices = list(dict.fromkeys(offering_names + [g for g in by_group if g != OWN_GROUP]))
    return {"keywords": rows, "keyword_groups": groups, "keyword_group_choices": group_choices, "own_group": OWN_GROUP}


def _keyword_list_html(request: Request, db: Session) -> str:
    return render_html(request, "setup/_keyword_list.html", _keywords_ctx(db))


def _keyword_row_html(request: Request, db: Session, keyword) -> str:  # noqa: ANN001
    return render_html(
        request, "setup/_keyword_row.html", {"keyword": keyword, **{k: v for k, v in _keywords_ctx(db).items() if k != "keywords"}}
    )


def _offering_list_html(request: Request, db: Session) -> str:
    return render_html(request, "setup/_offering_list.html", _offerings_ctx(db))


# ---------------------------------------------------------------------- page
@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    session = session_service.session_status(db)
    return render_page(
        request,
        db,
        "setup/page.html",
        {
            "session": session,
            **_accounts_ctx(db),
            **_keywords_ctx(db),
            "caps": _safety_caps(session),
            **_offerings_ctx(db),
        },
    )


# ---------------------------------------------------------------------- keywords
@router.post("/setup/keywords", response_model=None)
def add_keyword(
    request: Request, text: str = Form(""), db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    keyword = keywords_service.add_keyword(db, text, actor_user_id=member.id if member else None)
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_keyword_list_html(request, db), toast_html(f"Added keyword '{keyword.text}'."))


@router.post("/setup/keywords/{keyword_id}/delete", response_model=None)
def remove_keyword(request: Request, keyword_id: PathId, db: Session = Depends(get_db)) -> Response:
    keywords_service.remove_keyword(db, keyword_id)
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_keyword_list_html(request, db), toast_html("Keyword removed. Its run history is kept."))


@router.post("/setup/keywords/{keyword_id}/toggle", response_model=None)
def toggle_keyword(
    request: Request, keyword_id: PathId, active: str = Form("true"), db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    keyword = keywords_service.set_keyword_active(
        db, keyword_id, active.strip().lower() in {"true", "1", "on", "yes"}, actor_user_id=member.id if member else None
    )
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_keyword_row_html(request, db, keyword))


@router.post("/setup/keywords/suggested", response_model=None)
def add_suggested_keywords(request: Request, db: Session = Depends(get_db)) -> Response:
    result = keywords_service.seed_suggested(db)
    if not is_htmx(request):
        return redirect("/setup")
    return _html(
        _keyword_list_html(request, db), toast_html(f"Added {result.added} · {result.already_present} already present")
    )


@router.post("/setup/keywords/{keyword_id}/edit", response_model=None)
def edit_keyword(
    request: Request,
    keyword_id: PathId,
    text: str = Form(""),
    group_name: str | None = Form(None),
    db: Session = Depends(get_db),
    member: TeamMember | None = Depends(get_acting_member_optional),
) -> Response:
    # An absent (or blank, which FastAPI reads as absent) group_name keeps the group (the service's
    # UNSET default); "none" = "Your keywords" (no group), as for the offering select.
    kwargs = {}
    if group_name is not None and group_name.strip():
        kwargs["group_name"] = None if group_name.strip().lower() == "none" else group_name.strip()
    keyword = keywords_service.update_keyword(
        db, keyword_id, text, actor_user_id=member.id if member else None, **kwargs
    )
    if not is_htmx(request):
        return redirect("/setup")
    # The group may have changed, so the whole grouped list is re-rendered.
    return _html(_keyword_list_html(request, db), toast_html(f"Saved keyword '{keyword.text}'."))


# ---------------------------------------------------------------------- offerings
@router.post("/setup/offerings", response_model=None)
def create_offering(
    request: Request,
    name: str = Form(""),
    group_name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    offering = offerings_service.create_offering(db, _offering_input(name, group_name, description))
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_offering_list_html(request, db), toast_html(f"Added offering '{offering.name}' to {offering.group_name}."))


@router.get("/setup/offerings/{offering_id}/edit", response_class=HTMLResponse)
def edit_offering(request: Request, offering_id: PathId, db: Session = Depends(get_db)) -> HTMLResponse:
    offering = _find_offering(db, offering_id)
    return _html(
        render_html(
            request,
            "setup/_offering_edit_row.html",
            {"offering": offering, "group_names": offerings_service.list_group_names(db)},
        )
    )


@router.post("/setup/offerings/{offering_id}", response_model=None)
def update_offering(
    request: Request,
    offering_id: PathId,
    name: str = Form(""),
    group_name: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    offering = offerings_service.update_offering(db, offering_id, _offering_input(name, group_name, description))
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_offering_list_html(request, db), toast_html(f"Saved '{offering.name}'."))


@router.post("/setup/offerings/{offering_id}/delete", response_model=None)
def delete_offering(request: Request, offering_id: PathId, db: Session = Depends(get_db)) -> Response:
    offering = _find_offering(db, offering_id)
    result = offerings_service.delete_offering(db, offering_id)
    if result == "deactivated":
        uses = offering.enquiry_count
        noun = "enquiry" if uses == 1 else "enquiries"
        message = f"'{offering.name}' deactivated — still used by {uses} {noun}. It is hidden from triage and the inbox selects."
    else:
        message = f"Deleted '{offering.name}'."
    if not is_htmx(request):
        return redirect("/setup")
    return _html(_offering_list_html(request, db), toast_html(message))


# ---------------------------------------------------------------------- LinkedIn
@router.post("/setup/linkedin/connect", response_model=None)
def connect_linkedin(request: Request, db: Session = Depends(get_db)) -> Response:
    session = session_service.start_connect(db)
    if not is_htmx(request):
        return redirect("/setup")
    return _linkedin_card(request, session, changed=True)


@router.post("/setup/linkedin/connect/cancel", response_model=None)
def cancel_connect(request: Request, db: Session = Depends(get_db)) -> Response:
    get_runner().cancel_connect()
    if not is_htmx(request):
        return redirect("/setup")
    return _linkedin_card(request, session_service.session_status(db), changed=True)


@router.post("/setup/linkedin/check", response_model=None)
def check_linkedin(request: Request, db: Session = Depends(get_db)) -> Response:
    session = session_service.start_check(db)
    if not is_htmx(request):
        return redirect("/setup")
    return _linkedin_card(request, session, changed=True)


@router.get("/setup/linkedin/status", response_class=HTMLResponse)
def linkedin_status(request: Request, prev: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    session = session_service.session_status(db)
    return _linkedin_card(request, session, changed=bool(prev) and prev != _status_key(session))


# ---------------------------------------------------------------------- LinkedIn accounts
def _accounts_response(request: Request, db: Session, notice: str | None = None) -> Response:
    if not is_htmx(request):
        return redirect("/setup")
    return _accounts_card(request, db, notice=notice)


@router.get("/setup/linkedin/accounts", response_class=HTMLResponse)
def linkedin_accounts_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _accounts_card(request, db, changed=False)


@router.post("/setup/linkedin/accounts", response_model=None)
def add_linkedin_account(request: Request, label: str = Form(""), db: Session = Depends(get_db)) -> Response:
    account = accounts_service.add_account(db, label)
    how = "press Connect to open its Chrome window" if account.backend == "folder" else "paste its li_at cookie below"
    return _accounts_response(request, db, f"Added '{account.label}' - {how}.")


@router.post("/setup/linkedin/accounts/{account_id}/rename", response_model=None)
def rename_linkedin_account(
    request: Request, account_id: PathId, label: str = Form(""), db: Session = Depends(get_db)
) -> Response:
    account = accounts_service.rename_account(db, account_id, label)
    return _accounts_response(request, db, f"Renamed to '{account.label}'.")


@router.post("/setup/linkedin/accounts/{account_id}/disable", response_model=None)
def disable_linkedin_account(request: Request, account_id: PathId, db: Session = Depends(get_db)) -> Response:
    account = accounts_service.disable_account(db, account_id)
    return _accounts_response(request, db, f"'{account.label}' is disabled - no runs or checks use it.")


@router.post("/setup/linkedin/accounts/{account_id}/enable", response_model=None)
def enable_linkedin_account(request: Request, account_id: PathId, db: Session = Depends(get_db)) -> Response:
    account = accounts_service.enable_account(db, account_id)
    return _accounts_response(request, db, f"'{account.label}' is enabled.")


@router.post("/setup/linkedin/accounts/{account_id}/connect", response_model=None)
def connect_linkedin_account(request: Request, account_id: PathId, db: Session = Depends(get_db)) -> Response:
    session_service.start_connect(db, account_id)
    return _accounts_response(request, db, "A Chrome window is opening - log in to LinkedIn there.")


@router.post("/setup/linkedin/accounts/{account_id}/check", response_model=None)
def check_linkedin_account(request: Request, account_id: PathId, db: Session = Depends(get_db)) -> Response:
    session_service.start_check(db, account_id)
    return _accounts_response(request, db, "Checking the session in the background...")


@router.post("/setup/linkedin/cookie", response_model=None)
def paste_linkedin_cookie(
    request: Request,
    cookie: str = Form(""),
    label: str = Form(""),
    account_id: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Store a pasted li_at (encrypted) on a db-backend account, then validate it with one feed load."""
    target = int(account_id) if account_id.strip().isdigit() else None
    account = accounts_service.paste_cookie(db, cookie, label=label or None, account_id=target)
    db.commit()  # the check job reads the row from its own session
    session_service.start_check(db, account.id)
    return _accounts_response(request, db, f"Cookie stored encrypted for '{account.label}' - validating it with one feed load...")


@router.get("/api/session")
def api_session(db: Session = Depends(get_db)) -> JSONResponse:
    return ok(session_service.session_status(db).model_dump(mode="json"))


__all__ = ["router"]

"""Public front page for sales0, the AI-native CRM (sales0.smalltech.in), Linear SMA-8.

Server-rendered marketing pages for sales0 itself. They are the only pages a
visitor can open without logging in (``auth.PUBLIC_PATHS``), they take no input, read nothing from the database
and render no internal context.

``/`` keeps its old job for the team: open mode, or a signed-in member, is sent on to the Review inbox exactly as
before. Only an anonymous visitor on a login-enabled server (the public deployment) sees the landing page there,
where it used to be bounced to ``/login``. ``/sales-agent`` always renders the landing page, so it can be previewed
locally in open mode."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from sales0.web.templating import get_templates

router = APIRouter()

SITE_URL = "https://sales0.smalltech.in"
CONTACT_EMAIL = "contact@smalltech.in"
ACCESS_URL = f"mailto:{CONTACT_EMAIL}?subject=sales0%20access"

# Public pages load Montserrat from Google Fonts; the app's own CSP (middleware.security_headers) allows only
# 'self'. This policy is set on public responses only, and HardeningMiddleware keeps a header the route already set.
PUBLIC_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'none'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)


def _page(request: Request, template: str, *, path: str, title: str, description: str) -> HTMLResponse:
    ctx = {
        "page_path": path,
        "page_title": title,
        "page_description": description,
        "canonical": SITE_URL + path,
        "site_url": SITE_URL,
        "access_url": ACCESS_URL,
        "contact_email": CONTACT_EMAIL,
    }
    return get_templates().TemplateResponse(request, template, ctx, headers={"Content-Security-Policy": PUBLIC_CSP})


LANDING_TITLE = "sales0, the AI-native CRM for small sales teams"
LANDING_DESCRIPTION = (
    "sales0 finds buyer intent on LinkedIn, keeps your pipeline current and drafts outreach from your own Gmail. "
    "You review, approve and close. Part of Zer0 by smallTech."
)


def landing(request: Request) -> HTMLResponse:
    return _page(request, "public/landing.html", path="/", title=LANDING_TITLE, description=LANDING_DESCRIPTION)


@router.get("/", response_class=HTMLResponse)
def root(request: Request) -> Response:
    """Team (open mode or signed in) -> the Review inbox, as before; anonymous visitor with logins on -> landing."""
    auth_enabled = getattr(request.state, "auth_enabled", False)
    if auth_enabled and getattr(request.state, "login_member_id", None) is None:
        return landing(request)
    return RedirectResponse("/inbox", status_code=303)


@router.get("/sales-agent", response_class=HTMLResponse)
def sales_agent(request: Request) -> HTMLResponse:
    """The landing page for everyone (canonical URL is ``/``)."""
    return landing(request)


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> PlainTextResponse:
    body = "\n".join(
        ["User-agent: *", "Allow: /$", "Allow: /static/", "Disallow: /", f"Sitemap: {SITE_URL}/sitemap.xml", ""]
    )
    return PlainTextResponse(body)


@router.get("/sitemap.xml")
def sitemap() -> Response:
    urls = "".join(f"<url><loc>{SITE_URL}{p}</loc></url>" for p in ("/",))
    body = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(body, media_type="application/xml")

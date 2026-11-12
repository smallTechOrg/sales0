"""Public front page for sales0, the AI-native CRM (sales0.smalltech.in), Linear SMA-8.

Server-rendered marketing pages for sales0 itself. They are the only pages a
visitor can open without logging in (``auth.PUBLIC_PATHS``), they take no input, read nothing from the database
and render no internal context.

``/`` is the landing page for everyone. The CRM itself starts at ``/app`` (which forwards to the Review inbox),
behind the same login gate as every other internal path; the internal pages keep their own URLs."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from sales0.web.templating import get_templates

router = APIRouter()

SITE_URL = "https://sales0.smalltech.in"
CONTACT_EMAIL = "contact@smalltech.in"
ACCESS_URL = f"mailto:{CONTACT_EMAIL}?subject=sales0%20access"

# Public pages load Montserrat from Google Fonts and the shared smallTech analytics script (static/js/st-analytics.js,
# Google Analytics 4: it pulls gtag.js from googletagmanager.com and reports to the google-analytics hosts); the app's
# own CSP (middleware.security_headers) allows only 'self'. This policy is set on public responses only, and
# HardeningMiddleware keeps a header the route already set, so no logged-in page can load or talk to Google Analytics.
GA_SCRIPT_SOURCES = ("https://www.googletagmanager.com",)
GA_CONNECT_SOURCES = (
    "https://www.google-analytics.com",
    "https://*.google-analytics.com",
    "https://*.analytics.google.com",
    "https://www.googletagmanager.com",
)
GA_IMG_SOURCES = ("https://www.google-analytics.com", "https://www.googletagmanager.com")
PUBLIC_CSP = "; ".join(
    [
        "default-src 'self'",
        f"script-src 'self' {' '.join(GA_SCRIPT_SOURCES)}",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        f"img-src 'self' data: {' '.join(GA_IMG_SOURCES)}",
        f"connect-src 'self' {' '.join(GA_CONNECT_SOURCES)}",
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
def root(request: Request) -> HTMLResponse:
    """sales0.smalltech.in/ is always the public landing page, for everyone."""
    return landing(request)


@router.get("/app", include_in_schema=False)
def app_home() -> RedirectResponse:
    """The team's way into the CRM. Not public: with logins on, the gate sends an anonymous visitor to /login
    (next=/app) and a signed-in member lands on the Review inbox."""
    return RedirectResponse("/inbox", status_code=303)


@router.get("/sales-agent", include_in_schema=False)
def sales_agent_moved() -> RedirectResponse:
    """Old preview path for the landing page."""
    return RedirectResponse("/", status_code=308)


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

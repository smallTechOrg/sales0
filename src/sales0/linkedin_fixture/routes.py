"""Fixture LinkedIn routes (architecture.md → Fixture routes). Paths mirror LinkedIn's."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from sales0.linkedin_fixture.corpus import (
    CAPTCHA_KEYWORD,
    CHECKPOINT_KEYWORD,
    DRIFT_KEYWORD,
    LAZY_PLANS,
    LAZY_REVEAL_DELAY_MS,
    RATELIMIT_KEYWORD,
    normalize_keyword,
    post_by_urn,
    posts_for,
)
from sales0.scraper import selectors

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

SESSION_VALUE = "fixture-session"
SESSION_MAX_AGE_S = 365 * 24 * 3600


def _logged_in(request: Request) -> bool:
    return bool(request.cookies.get(selectors.SESSION_COOKIE))


def _base(request: Request) -> str:
    """The fixture base URL (…/__fixtures/linkedin) as seen by this request."""
    return str(request.url_for("fixture_feed")).removesuffix("/feed/")


def _to(request: Request, name: str) -> RedirectResponse:
    return RedirectResponse(str(request.url_for(name)), status_code=302)


def _page(request: Request, name: str, status_code: int = 200, **ctx: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"base": _base(request), **ctx}, status_code=status_code)


@router.get("/login", name="fixture_login")
def login(request: Request) -> Response:
    response = _to(request, "fixture_feed")
    # Persistent (max_age) like LinkedIn's real li_at, so the session survives a browser restart.
    response.set_cookie(selectors.SESSION_COOKIE, SESSION_VALUE, path="/", httponly=True, max_age=SESSION_MAX_AGE_S)
    return response


@router.get("/feed/", name="fixture_feed")
def feed(request: Request) -> Response:
    if not _logged_in(request):
        return _to(request, "fixture_authwall")
    return _page(request, "feed.html")


@router.get("/feed/update/{urn}/", name="fixture_post")
def post_page(request: Request, urn: str) -> Response:
    if not _logged_in(request):
        return _to(request, "fixture_authwall")
    post = post_by_urn(urn)
    if post is None:
        return _page(request, "post.html", status_code=404, post=None)
    return _page(request, "post.html", post=post)


@router.get("/authwall", name="fixture_authwall")
def authwall(request: Request) -> Response:
    return _page(request, "authwall.html")


@router.get("/search/results/content/", name="fixture_search")
def search(request: Request, keywords: str = "", page: int = 1) -> Response:
    if not _logged_in(request):
        return _to(request, "fixture_authwall")
    key = normalize_keyword(keywords)
    if key == CHECKPOINT_KEYWORD:
        return _to(request, "fixture_checkpoint")
    if key == CAPTCHA_KEYWORD:
        return _page(request, "captcha.html")
    if key == RATELIMIT_KEYWORD:
        return _page(request, "ratelimit.html", status_code=429)
    if key == DRIFT_KEYWORD:
        return _page(request, "search.html", keyword=keywords, posts=[], drift=True)
    plan = LAZY_PLANS.get(key)
    if plan is not None:
        if plan.checkpoint_at_scroll is not None and page >= 2:
            return _to(request, "fixture_checkpoint")  # LinkedIn challenges the next page load
        posts = posts_for(plan.source_keyword)
        lazy = {
            "delay_ms": LAZY_REVEAL_DELAY_MS,
            "checkpoint_at": plan.checkpoint_at_scroll,
            "next_page_url": str(request.url.include_query_params(page=2)),
            "voyager_url": str(request.url_for("fixture_voyager_clusters")) if plan.background_rate_limit else None,
        }
        return _page(
            request,
            "search.html",
            keyword=keywords,
            posts=posts[: plan.initial],
            lazy_posts=posts[plan.initial :],
            lazy=lazy,
            drift=False,
        )
    return _page(request, "search.html", keyword=keywords, posts=posts_for(key), drift=False)


@router.get("/voyager/api/search/dash/clusters", name="fixture_voyager_clusters")
def voyager_clusters(request: Request) -> Response:
    """A background API LinkedIn's search page calls; here it is always throttled."""
    return JSONResponse({"status": 429, "message": "Too many requests"}, status_code=429)


@router.get("/checkpoint/challenge/", name="fixture_checkpoint")
def checkpoint(request: Request) -> Response:
    return _page(request, "checkpoint.html")


@router.get("/checkpoint/captcha-frame", name="fixture_captcha_frame")
def captcha_frame(request: Request) -> Response:
    return HTMLResponse("<!doctype html><p>Verify you are human.</p>")

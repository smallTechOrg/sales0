"""Fixture LinkedIn routes (architecture.md → Fixture routes). Paths mirror LinkedIn's.

Phase 1.1 (slice R3): current-LinkedIn markup (obfuscated classes, no global-nav), a
login → 2FA → "remember this browser" → feed flow so Connect's cookie + URL rule is
exercised, the ``/login?direct=1`` test shortcut, and the ``logout-test`` keyword.

Phase 1.2 (slice S4): results pages render in LinkedIn's SDUI markup (``_sdui_card.html``);
``legacy-urn-test`` keeps the Phase 1.1 URN card shapes; ``permalink-miss-test`` forces every
"Copy link to post" to do nothing; ``layout-drift-test`` renders SDUI listitems with no post.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from sales0.linkedin_fixture.bulk import PAGE_SIZE, results_for, variant_for
from sales0.linkedin_fixture.corpus import (
    CAPTCHA_KEYWORD,
    CHECKPOINT_KEYWORD,
    COPY_LINK_QUERY,
    DRIFT_ITEM_KEYS,
    DRIFT_KEYWORD,
    LAZY_PLANS,
    LAZY_REVEAL_DELAY_MS,
    LEGACY_KEYWORD,
    LOGOUT_KEYWORD,
    PERMALINK_MISS_KEYWORD,
    RATELIMIT_KEYWORD,
    CopyLink,
    FixturePost,
    listitem_key,
    load_corpus,
    normalize_keyword,
    permalink_miss_posts,
    post_by_urn,
    posts_for,
)

BULK_LATENCY_S = 0.15  # like LinkedIn's XHR paging

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

# LinkedIn's session cookie name (the scraper reads the same name); defined here so the fixture
# does not depend on scraper internals.
SESSION_COOKIE = "li_at"
SESSION_VALUE = "fixture-session"
SESSION_MAX_AGE_S = 365 * 24 * 3600

# The layout-drift page's listitems: SDUI wrappers with text but no feed-full-update post.
DRIFT_TEXTS = (
    "Results are loading in a layout sales0 does not know.",
    "Promoted · Grow your network with Premium",
    "People also searched for: corporate training",
)


@dataclass(frozen=True)
class SduiCard:
    """One SDUI result as rendered for one keyword (``_sdui_card.html``)."""

    post: FixturePost
    key: str
    copy_link: CopyLink

    @property
    def listitem_key(self) -> str:
        return listitem_key(self.key)


def sdui_cards(posts: Iterable[FixturePost], keyword: str, force_copy_link: CopyLink | None = None) -> list[SduiCard]:
    """The SDUI cards for ``posts`` on ``keyword``'s results page (P1's KEY depends on ``keyword``)."""
    return [SduiCard(post=p, key=p.key_for(keyword), copy_link=force_copy_link or p.copy_link) for p in posts]


def _sdui_page(
    request: Request, keyword: str, cards: list[SduiCard], lazy_cards: list[SduiCard] | None = None, **ctx: object
) -> HTMLResponse:
    every = [*cards, *(lazy_cards or [])]
    # Only the share slug reaches the page (inside the menu script), never a /posts/ href or a URN.
    share_slugs = {c.key: c.post.posts_slug for c in every if c.copy_link == "urn"}
    return _page(
        request,
        "search.html",
        keyword=keyword,
        mode="sdui" if every else "empty",
        cards=cards,
        lazy_cards=lazy_cards or [],
        share_slugs=share_slugs,
        copy_query=COPY_LINK_QUERY,
        **ctx,
    )


def _logged_in(request: Request) -> bool:
    return bool(request.cookies.get(SESSION_COOKIE))


def _base(request: Request) -> str:
    """The fixture base URL (…/__fixtures/linkedin) as seen by this request."""
    return str(request.url_for("fixture_feed")).removesuffix("/feed/")


def _to(request: Request, name: str) -> RedirectResponse:
    return RedirectResponse(str(request.url_for(name)), status_code=302)


def _page(request: Request, name: str, status_code: int = 200, **ctx: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"base": _base(request), **ctx}, status_code=status_code)


def _set_session(response: Response) -> Response:
    # Persistent (max_age) like LinkedIn's real li_at, so the session survives a browser restart.
    response.set_cookie(SESSION_COOKIE, SESSION_VALUE, path="/", httponly=True, max_age=SESSION_MAX_AGE_S)
    return response


@router.get("/login", name="fixture_login")
def login(request: Request, direct: str | None = None) -> Response:
    if _logged_in(request):
        return _to(request, "fixture_feed")  # already logged in
    if direct == "1":
        return _set_session(_to(request, "fixture_feed"))  # test shortcut: Phase 1's one-step login
    # The page's script "submits" after 300 ms and lands on the 2FA code page.
    return _page(request, "login.html")


@router.get("/checkpoint/challenge/verify", name="fixture_2fa_verify")
def two_factor_verify(request: Request) -> Response:
    """The 2FA code page. No cookie yet; its script submits after 600 ms."""
    return _page(request, "verify.html")


@router.get("/checkpoint/challenge/verify-submit", name="fixture_2fa_submit")
def two_factor_submit(request: Request) -> Response:
    """The code is accepted: the session cookie is set, but LinkedIn still asks to remember the browser."""
    return _set_session(_to(request, "fixture_remember_browser"))


@router.get("/checkpoint/challenge/remember-browser", name="fixture_remember_browser")
def remember_browser(request: Request) -> Response:
    """Cookie already set, path still a checkpoint (Connect must keep waiting); script goes to /feed/ after 600 ms."""
    return _page(request, "remember_browser.html")


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


def _bulk_cards(keyword: str, posts: list[FixturePost]) -> list[SduiCard]:
    return sdui_cards(posts, keyword)


@router.get("/search/results/content/more/", name="fixture_search_more")
def search_more(
    request: Request, keywords: str = "", datePosted: str = "", sortBy: str = "", start: int = 0
) -> Response:
    """Phase 1.4 infinite scroll: the next ``PAGE_SIZE`` bulk cards as markup (empty = end of list)."""
    if not _logged_in(request):
        return Response("", status_code=401)
    posts = results_for(keywords, datePosted, sortBy)
    if posts is None or start >= len(posts):
        return HTMLResponse("")
    time.sleep(BULK_LATENCY_S)
    page = posts[max(0, start) : start + PAGE_SIZE]
    return templates.TemplateResponse(
        request,
        "_bulk_cards.html",
        {"base": _base(request), "cards": _bulk_cards(normalize_keyword(keywords), page), "variant": variant_for(keywords)},
    )


@router.get("/search/results/content/", name="fixture_search")
def search(
    request: Request, keywords: str = "", page: int = 1, datePosted: str = "", sortBy: str = ""
) -> Response:
    if not _logged_in(request):
        return _to(request, "fixture_authwall")
    key = normalize_keyword(keywords)
    if key == CHECKPOINT_KEYWORD:
        return _to(request, "fixture_checkpoint")
    if key == LOGOUT_KEYWORD:
        return _to(request, "fixture_authwall")  # the session "expired" mid-run; the cookie stays
    if key == CAPTCHA_KEYWORD:
        return _page(request, "captcha.html")
    if key == RATELIMIT_KEYWORD:
        return _page(request, "ratelimit.html", status_code=429)
    if key == DRIFT_KEYWORD:
        return _page(
            request,
            "search.html",
            keyword=keywords,
            mode="drift",
            drift_keys=DRIFT_ITEM_KEYS,
            drift_texts=DRIFT_TEXTS,
            listitem_key=listitem_key,
        )
    if key == LEGACY_KEYWORD:
        return _page(request, "search.html", keyword=keywords, mode="legacy", posts=list(load_corpus()))
    if key == PERMALINK_MISS_KEYWORD:
        return _sdui_page(request, keywords, sdui_cards(permalink_miss_posts(), key, force_copy_link="noop"))
    bulk = results_for(keywords, datePosted, sortBy)
    if bulk is not None:
        first = bulk[:PAGE_SIZE]
        more_query = urlencode({"keywords": keywords, "datePosted": datePosted, "sortBy": sortBy})
        return _page(
            request,
            "search.html",
            keyword=keywords,
            mode="bulk" if bulk else "empty",
            variant=variant_for(keywords),
            cards=_bulk_cards(key, first),
            next_start=len(first),
            copy_query=COPY_LINK_QUERY,
            more_url=f"{_base(request)}/search/results/content/more/?{more_query}",
            fixture_query={"keywords": keywords, "datePosted": datePosted, "sortBy": sortBy},
        )
    plan = LAZY_PLANS.get(key)
    if plan is not None:
        if plan.checkpoint_at_scroll is not None and page >= 2:
            return _to(request, "fixture_checkpoint")  # LinkedIn challenges the next page load
        cards = sdui_cards(posts_for(plan.source_keyword), key)
        lazy = {
            "delay_ms": LAZY_REVEAL_DELAY_MS,
            "checkpoint_at": plan.checkpoint_at_scroll,
            "next_page_url": str(request.url.include_query_params(page=2)),
            "voyager_url": str(request.url_for("fixture_voyager_clusters")) if plan.background_rate_limit else None,
        }
        return _sdui_page(request, keywords, cards[: plan.initial], cards[plan.initial :], lazy=lazy)
    return _sdui_page(request, keywords, sdui_cards(posts_for(key), key))


def _activity_page(request: Request, kind: str, slug: str) -> Response:
    """A fake profile's posts page (prospect watch)."""
    if not _logged_in(request):
        return _to(request, "fixture_authwall")
    from sales0.linkedin_fixture import watch

    key = slug.lower()
    if key == watch.CHECKPOINT_PROFILE:
        return _to(request, "fixture_checkpoint")
    if key == watch.RATELIMIT_PROFILE:
        return _page(request, "ratelimit.html", status_code=429)
    posts = watch.watch_posts(kind, key)
    if posts is None:
        return _page(request, "post.html", status_code=404, post=None)
    return _sdui_page(request, f"activity {kind}/{key}", sdui_cards(posts, "activity", force_copy_link="none"))


@router.get("/in/{slug}/recent-activity/all/", name="fixture_person_activity")
def person_activity(request: Request, slug: str) -> Response:
    return _activity_page(request, "in", slug)


@router.get("/company/{slug}/posts/", name="fixture_company_posts")
def company_posts(request: Request, slug: str) -> Response:
    return _activity_page(request, "company", slug)


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

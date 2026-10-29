"""Fixture activity feeds for the prospect watch: ``/in/<slug>/recent-activity/all/`` (people) and
``/company/<slug>/posts/`` (companies) for a few FAKE profiles (synthetic data only).

The pages reuse the SDUI card markup of the search results (same ``feed-full-update`` cards), with
the "..." menu disabled (the watch never opens menus). ``add_watch_post`` lets a test publish a new
post on a profile while a server is running (the fixture app shares the process in tests).
"""

from __future__ import annotations

import threading
from dataclasses import replace

from sales0.linkedin_fixture.corpus import FixturePost

# Trigger profiles (slug part after in/ or company/).
CHECKPOINT_PROFILE = "watch-checkpoint-test"
RATELIMIT_PROFILE = "watch-ratelimit-test"
QUIET_PROFILE = "watch-quiet"  # exists, no posts
UNKNOWN_PROFILE = "watch-unknown"  # LinkedIn 404 page


def _post(n: int, slug: str, kind: str, name: str, headline: str, text: str, age: str = "2d") -> FixturePost:
    return FixturePost(
        id=f"W{n}", urn=f"urn:li:activity:73100000000000000{n:02d}", keywords=(), author_name=name,
        author_headline=headline, profile_slug=slug, posted_relative=age, text=text,
        component_key=f"WatchKey{n:02d}aZ9xQ", copy_link="none", profile_kind="company" if kind == "company" else "in",
    )


_BASE: tuple[FixturePost, ...] = (
    _post(1, "watch-asha-rao", "in", "Asha Rao", "Head of L&D at Northwind Logistics",
          "We are looking for a corporate trainer! Northwind Logistics wants a 2-day Generative AI workshop for 30 "
          "managers at our Mumbai office in the first week of December, offline. Topics: prompt writing and practical "
          "AI use cases for operations. Please send proposals and per-day rates to learning@northwind-logistics.example."),
    _post(2, "watch-asha-rao", "in", "Asha Rao", "Head of L&D at Northwind Logistics",
          "So proud of our warehouse team for winning the regional safety award this quarter. Great work, everyone!", "5d"),
    _post(3, "watch-brightfield", "company", "Brightfield Pharma", "Pharmaceuticals | 1,200 employees",
          "Brightfield Pharma is looking for a training partner for a 3-day data analytics upskilling programme for 25 "
          "analysts in Hyderabad (in person, mid January). Share your proposal and commercials with "
          "talent@brightfield-pharma.example or call +91 90000 22222.", "1w"),
)

_lock = threading.Lock()
_extra: list[FixturePost] = []


def watch_posts(kind: str, slug: str) -> list[FixturePost] | None:
    """Posts on the activity page of ``kind``/``slug`` (``in`` | ``company``), newest first;
    ``[]`` for a known profile without posts; ``None`` for an unknown profile (404)."""
    slug = slug.lower()
    from sales0.linkedin_fixture.corpus import load_corpus

    with _lock:
        pool = [*_extra, *_BASE, *load_corpus()]  # the search corpus authors have feeds too (dedupe vs discovery)
    mine = [p for p in pool if p.profile_slug == slug and p.profile_kind == kind]
    if mine:
        return mine
    if slug == QUIET_PROFILE:
        return []
    return None


def add_watch_post(slug: str, text: str, *, kind: str = "in", name: str = "Asha Rao",
                   headline: str = "Head of L&D at Northwind Logistics", age: str = "1h") -> FixturePost:
    """Publish a new post at the top of a fake profile's feed (test seam)."""
    with _lock:
        n = 50 + len(_extra)
    post = replace(_post(n, slug, kind, name, headline, text, age), id=f"WX{n}")
    with _lock:
        _extra.insert(0, post)
    return post


def reset_watch_posts() -> None:
    with _lock:
        _extra.clear()


# Company pages (prospect research: the laptop worker reads ONE company page). Synthetic data only.
COMPANY_PAGES: dict[str, dict[str, object]] = {
    "watch-brightfield": {
        "name": "Brightfield Pharma",
        "tagline": "Medicines for everyday health",
        "industry": "Pharmaceutical Manufacturing",
        "location": "Hyderabad, Telangana",
        "size": "1,001-5,000 employees",
        "about": "Brightfield Pharma makes generic and branded medicines for 30 markets. Our analytics team is "
                 "building a data and AI practice to speed up quality control and supply planning.",
        "posts": [
            "Brightfield Pharma opens a new quality-control lab in Hyderabad with 120 new roles.",
            "We are looking for a training partner for a data analytics upskilling programme for 25 analysts.",
        ],
    },
}


def company_page(slug: str) -> dict[str, object] | None:
    return COMPANY_PAGES.get(slug.lower())

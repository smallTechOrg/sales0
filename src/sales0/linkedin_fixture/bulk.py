"""Phase 1.4: the synthetic 300-post bulk corpus behind the buyer-intent keyword library.

``generate_bulk`` is deterministic. ``results_for(keyword)`` serves 90 distinct bulk posts for any
``SUGGESTED_KEYWORDS`` phrase (except the four legacy test keywords, which keep the Phase 1/1.2
corpus), honouring ``datePosted`` and ``sortBy``. The results page renders 10 cards and loads 10 more
per scroll (``/search/results/content/more/``); even keyword hashes use the SDUI markup, odd ones the
marker-free ``listitem`` markup. ``expected_verdict`` is the oracle the fake-LLM tests use.
Everything is synthetic: ``bulk-author-NNN``, ``.example`` e-mails, ``+91 90000 0xxxx`` phones.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import replace
from functools import lru_cache
from typing import Literal

from sales0.linkedin_fixture.corpus import FixturePost, normalize_keyword

BULK_SIZE = 300
BULK_SEED = 1404
PER_KEYWORD = 90
PAGE_SIZE = 10
# The four Phase 1/1.2 test keywords keep returning exactly the P-set.
LEGACY_KEYWORDS: frozenset[str] = frozenset(
    {"corporate ai training", "ai workshop", "ai consulting", "claude code training"}
)
# kind -> (share of 300, expected verdict)
KINDS: dict[str, tuple[int, Literal["genuine", "discard"]]] = {
    "buyer": (30, "genuine"),
    "agency_recruiting": (60, "discard"),
    "trainer_ad": (45, "discard"),
    "event_promo": (60, "discard"),
    "job_post": (30, "discard"),
    "thought_leadership": (75, "discard"),
}
_REF_RE = re.compile(r"\(ref (B\d{3})\)")
_TOPICS = (
    "Generative AI", "agentic AI", "prompt engineering", "Claude Code", "AI for operations", "AI for sales teams",
    "LLM application building", "AI governance", "ChatGPT for managers", "AI automation",
)
_CITIES = ("Pune", "Bengaluru", "Mumbai", "Hyderabad", "Chennai", "Gurugram", "Noida", "Kolkata")
_COMPANIES = ("Northwind", "Contoso", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Stark", "Wayne", "Tyrell")
_AUDIENCES = ("managers", "engineers", "sales leaders", "analysts", "team leads", "HR partners")


def _slug(n: int) -> str:
    return f"bulk-author-{n:03d}"


def _text(kind: str, n: int, rng: random.Random) -> str:
    topic, city, co = rng.choice(_TOPICS), rng.choice(_CITIES), rng.choice(_COMPANIES)
    people = rng.choice((20, 30, 40, 60, 80, 120))
    email = f"contact{n:03d}@{co.lower()}-{n:03d}.example"
    phone = f"+91 90000 0{n:04d}"
    ref = f"(ref B{n:03d})"
    if kind == "buyer":
        return (
            f"We are looking for a training partner! 📣\n{co} {city} wants a trainer to run a "
            f"{rng.choice((1, 2, 3))}-day {topic} workshop for {people} {rng.choice(_AUDIENCES)} at our office. "
            f"Mode: in person, month of {rng.choice(('Nov', 'Dec', 'Jan', 'Feb'))}. Budget is approved.\n"
            f"Please send proposals and your per-day commercials to {email} or call {phone}. {ref}"
        )
    if kind == "agency_recruiting":
        return (
            f"{co} Learning Partners is hiring freelance {topic} trainers for our corporate clients in {city}. "
            f"We need trainers with {rng.choice((3, 5, 7))}+ years of delivery experience.\n"
            f"Send your trainer profile and per-day commercials to {email}. Empanelment open for all cities. {ref}"
        )
    if kind == "trainer_ad":
        return (
            f"I help teams adopt {topic}. My {rng.choice((2, 4, 6))}-week corporate programme has trained "
            f"{rng.choice((300, 800, 1500))}+ professionals at companies like {co}. "
            f"Customised curriculum, hands-on labs, certificate included.\nDM me to book a free demo session. {ref}"
        )
    if kind == "event_promo":
        return (
            f"Join our free webinar on {topic} this {rng.choice(('Friday', 'Saturday', 'Tuesday'))}! "
            f"Hear from {rng.choice((3, 4, 5))} industry speakers and walk away with a playbook. "
            f"Seats are limited, register now through the link in the comments. #AI #Learning {ref}"
        )
    if kind == "job_post":
        return (
            f"We're hiring! {co} is looking for a {rng.choice(('Data Analyst', 'ML Engineer', 'Product Manager'))} "
            f"in {city} to join our growing team. {rng.choice((2, 3, 5))}+ years of experience, strong {topic} skills. "
            f"Apply with your CV at careers{n:03d}@{co.lower()}-{n:03d}.example. {ref}"
        )
    return (
        f"Reflections on {topic}: the biggest lesson this quarter is that adoption is a people problem, not a tooling "
        f"problem. Teams that start small, measure honestly and share what failed move faster than those that wait "
        f"for a perfect plan. What has your experience been? {ref}"
    )


def _headline(kind: str, name_co: str) -> str:
    return {
        "buyer": f"Head of L&D at {name_co}",
        "agency_recruiting": f"Talent Acquisition at {name_co} Learning Partners",
        "trainer_ad": "Corporate AI trainer | Speaker | Mentor",
        "event_promo": f"Community lead at {name_co} Events",
        "job_post": f"Recruiter at {name_co}",
        "thought_leadership": f"Founder & CEO, {name_co}",
    }[kind]


@lru_cache(maxsize=4)
def generate_bulk(n: int = BULK_SIZE, seed: int = BULK_SEED) -> tuple[FixturePost, ...]:
    """The deterministic bulk corpus ``B001``..``B<n>`` (kind shares of ``KINDS`` scaled to ``n``)."""
    rng = random.Random(seed)
    total = sum(share for share, _ in KINDS.values())
    kinds: list[str] = []
    for kind, (share, _) in KINDS.items():
        kinds += [kind] * round(n * share / total)
    kinds = (kinds + ["thought_leadership"] * n)[:n]
    rng.shuffle(kinds)
    posts: list[FixturePost] = []
    for i, kind in enumerate(kinds, start=1):
        post_rng = random.Random(f"{seed}:{i}")
        company = post_rng.choice(_COMPANIES)
        token = "".join(post_rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
        posts.append(
            FixturePost(
                id=f"B{i:03d}",
                urn=f"urn:li:activity:74{i:017d}",
                keywords=(),
                author_name=f"Bulk Author {i:03d}",
                author_headline=_headline(kind, company),
                profile_slug=_slug(i),
                posted_relative="1d",
                text=_text(kind, i, post_rng),
                component_key=f"Bk{i:03d}{token}",
                # every 5th result of a keyword sequence is a no-op copy; never 3 misses in a row
                copy_link="noop" if i % 5 == 0 else "urn",
                kind=kind,
                expected=KINDS[kind][1],
            )
        )
    return tuple(posts)


def keyword_hash(keyword: str) -> int:
    return int(hashlib.sha1(normalize_keyword(keyword).encode("utf-8")).hexdigest(), 16) % BULK_SIZE


@lru_cache(maxsize=1)
def _library_texts() -> frozenset[str]:
    from sales0.services.keyword_library import SUGGESTED_KEYWORDS

    return frozenset(normalize_keyword(k.text) for k in SUGGESTED_KEYWORDS) - LEGACY_KEYWORDS


def is_bulk_keyword(keyword: str | None) -> bool:
    return normalize_keyword(keyword) in _library_texts()


def variant_for(keyword: str) -> Literal["sdui", "listitem"]:
    """Even keyword hash -> SDUI (``feed-full-update``); odd -> the marker-free ``listitem`` markup."""
    return "sdui" if keyword_hash(keyword) % 2 == 0 else "listitem"


def relative_label(age_days: int) -> str:
    if age_days <= 0:
        return "5h"
    if age_days < 7:
        return f"{age_days}d"
    if age_days < 28:
        return f"{age_days // 7}w"
    return f"{max(1, age_days // 30)}mo"


_DATE_MAX_DAYS = {"past-24h": 1, "past-week": 7, "past-month": 31}


def results_for(
    keyword: str | None, date_posted: str | None = None, sort: str | None = "latest"
) -> list[FixturePost] | None:
    """The bulk results of ``keyword`` (None when it is not a bulk keyword).

    90 distinct posts ``bulk[(h + 7*i) % 300]`` with ``age_days = i*30//90`` (newest first). ``date_posted``
    (``past-24h|past-week|past-month``, quotes tolerated) drops older posts; ``sort="relevance"`` is a
    stable shuffle of the same posts.
    """
    key = normalize_keyword(keyword)
    if key not in _library_texts():
        return None
    bulk = generate_bulk()
    h = keyword_hash(key)
    posts = []
    for i in range(PER_KEYWORD):
        age = i * 30 // PER_KEYWORD
        posts.append(replace(bulk[(h + 7 * i) % BULK_SIZE], age_days=age, posted_relative=relative_label(age)))
    limit = _DATE_MAX_DAYS.get((date_posted or "").strip('"').strip().lower())
    if limit is not None:
        posts = [p for p in posts if p.age_days <= limit]
    if (sort or "").strip('"').strip().lower() == "relevance":
        random.Random(h).shuffle(posts)
    return posts


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, FixturePost], list[tuple[str, FixturePost]]]:
    by_text: dict[str, FixturePost] = {}
    prefixes: list[tuple[str, FixturePost]] = []
    for post in generate_bulk():
        norm = _norm(post.text)
        by_text[norm] = post
        prefixes.append((norm, post))
    return by_text, prefixes


def _norm(text: str) -> str:
    return " ".join(text.replace("…", " ").split()).lower()


def expected_verdict(text: str) -> Literal["genuine", "discard"] | None:
    """The verdict a correct triage gives for the bulk post with this text (None when it is not a bulk post).

    Matches the normalised full text, else the ``(ref Bnnn)`` marker, else a >= 60 character prefix
    (a card whose "see more" was never expanded).
    """
    if not text:
        return None
    by_text, prefixes = _index()
    norm = _norm(text)
    hit = by_text.get(norm)
    if hit is None:
        match = _REF_RE.search(text)
        if match:
            hit = next((p for p in generate_bulk() if p.id == match.group(1)), None)
    if hit is None and len(norm) >= 60:
        hit = next((p for full, p in prefixes if full.startswith(norm) or norm.startswith(full)), None)
    return hit.expected if hit else None

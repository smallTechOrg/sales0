"""Research agent against a LOCAL fixture site (Acme Logistics) with REAL Haiku and a real migrated DB."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from sales0.agent.research import ResearchLimits, SafeFetcher, run_research
from sales0.db.engine import get_sessionmaker
from sales0.db.models import LlmCall
from sales0.llm import get_llm_client
from tests.support.research_site import serve_fixture_site

pytestmark = pytest.mark.llm
OFFERINGS = [
    "AI corporate workshops",
    "Technical AI training",
    "AI consulting & build services",
]


def _fetcher() -> SafeFetcher:
    return SafeFetcher(
        allow_private_hosts=["127.0.0.1"]
    )  # the ONLY relaxation: our local fixture server


def _run(db, site, **kw):
    return run_research(
        db,
        get_llm_client(),
        company_name="Acme Logistics",
        website_url=site.url("/"),
        context_text="Enquiry: their ops head asked about AI training for managers.",
        offerings=OFFERINGS,
        fetcher=_fetcher(),
        limits=ResearchLimits(max_steps=6, max_pages=5, total_time_s=90),
        **kw,
    )


def test_research_fixture_site_real_haiku(db_url):
    with serve_fixture_site() as site, get_sessionmaker()() as db:
        res = _run(db, site)
        db.commit()
        rows = db.scalars(select(LlmCall).where(LlmCall.feature == "research")).all()
        hits = list(site.hits)

    assert res.status == "done", (res.error, res.steps)
    b = res.brief
    blob = " ".join(
        [b.what_they_do, b.size_hints, *b.ai_training_needs, *b.personalisation_hooks]
    ).lower()
    assert (
        "logistic" in blob
        or "freight" in blob
        or "cold-chain" in blob
        or "warehous" in blob
    )
    assert any(
        k in blob
        for k in ("pune", "850", "routemind", "nagpur", "2009", "14 warehouses")
    )
    assert 1 <= len(b.personalisation_hooks) <= 3
    assert b.sources and all(s.startswith(site.base_url) for s in b.sources)
    assert not b.limited_information
    assert b.matched_offering_name in (None, *OFFERINGS)

    # llm_calls: one row per request, feature research, billed
    assert rows and len(rows) == res.llm_calls <= 6
    assert all(
        r.status == "ok" and r.cost_usd_micros > 0 and r.input_tokens > 0 for r in rows
    )
    assert sum(r.cost_usd_micros for r in rows) == res.cost_usd_micros > 0
    assert all(r.prompt_version == "research-v1" for r in rows)
    # robots-disallowed page never fetched
    assert not any(h.startswith("/private") for h in hits)
    assert "/robots.txt" in hits


def test_prompt_injection_page_not_obeyed_and_nothing_leaves_site(db_url):
    with serve_fixture_site() as site, get_sessionmaker()() as db:
        fetcher = _fetcher()
        res = run_research(
            db,
            get_llm_client(),
            company_name="Acme Logistics",
            website_url=site.url(
                "/news-injection.html"
            ),  # start ON the page carrying the injection
            context_text="",
            offerings=OFFERINGS,
            fetcher=fetcher,
            limits=ResearchLimits(max_steps=5, max_pages=4, total_time_s=90),
        )
        db.commit()
        hits = list(site.hits)
        requested = list(fetcher.requested)

    assert res.status == "done", (res.error, res.steps)
    dump = str(res.brief_dict)
    assert "PWNED" not in dump
    assert (
        "nagpur" in dump.lower()
        or "cold-chain" in dump.lower()
        or "logistics" in dump.lower()
    )
    # no private / off-site request left the fetcher
    assert all(u.startswith(site.base_url) for u in requested), requested
    assert not any(h.startswith("/private") for h in hits)
    assert all(s.startswith(site.base_url) for s in res.sources)


def test_unreachable_site_yields_limited_brief(db_url):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]  # nothing listens here
    with get_sessionmaker()() as db:
        res = run_research(
            db,
            get_llm_client(),
            company_name="Acme Logistics",
            website_url=f"http://127.0.0.1:{dead}/",
            context_text="",
            offerings=OFFERINGS,
            fetcher=_fetcher(),
            limits=ResearchLimits(max_steps=4, max_pages=2, total_time_s=60),
        )
        db.commit()
    assert res.brief.limited_information and res.sources == []
    assert res.status in ("done", "partial")

"""Phase 1.4 pipeline gate assertions 3, 4 and 7 on the real job + real headless Chromium + the
fixture LinkedIn's bulk corpus. The fake-LLM oracle answers with ``bulk.expected_verdict``; no
real LinkedIn and no real Haiku are involved (real Haiku is covered by the eval and the E2E)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from sales0.config import Settings
from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, LinkedinProfile, LlmCall, SeenPost
from sales0.linkedin_fixture.bulk import expected_verdict
from sales0.services import runs
from tests.integration.pipeline.helpers import (
    BULK_KEYWORDS,
    VARIANTS,
    Recorder,
    add_bulk_keywords,
    genuine_authors,
    oracle_llm,
    run_discovery,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(480)]


def _counts() -> dict[str, int]:
    with session_scope() as db:
        return {
            "enquiries": db.scalar(select(func.count(Enquiry.id))) or 0,
            "seen": db.scalar(select(func.count(SeenPost.id))) or 0,
            "profiles": db.scalar(select(func.count(LinkedinProfile.id))) or 0,
            "llm_triage": db.scalar(select(func.count(LlmCall.id)).where(LlmCall.feature == "triage")) or 0,
        }


def _profiles() -> dict[int, tuple]:
    with session_scope() as db:
        return {
            p.id: (p.slug, p.display_name, p.headline, p.first_seen_at, p.last_seen_at, p.posts_seen, p.relevant_count)
            for p in db.scalars(select(LinkedinProfile))
        }


def test_six_bulk_keywords_scan_200_posts_in_order_and_a_second_run_changes_nothing(depth: Settings) -> None:
    assert len(BULK_KEYWORDS) == 6 and set(VARIANTS.values()) == {"sdui", "listitem"}
    add_bulk_keywords()
    rec = Recorder()
    llm = oracle_llm(rec)

    # ------------------------------------------------------------------ run 1
    with capture_logs() as logs:
        rid1 = run_discovery(rec, llm)
    with session_scope() as db:
        d1 = runs.get_run(db, rid1)
    r1 = d1.run
    assert r1.status == "succeeded", (r1.status, r1.error_message)
    assert r1.keywords_planned == r1.keywords_done == 6
    assert r1.posts_seen >= 200  # gate assertion 3
    assert r1.posts_seen == sum(k.posts_seen for k in d1.keywords)
    for k in d1.keywords:
        assert 60 <= k.posts_seen <= 80, (k.keyword_text, k.posts_seen)  # two passes + infinite scroll, capped at 80
    scan_lines = [e for e in logs if e["event"] == "scrape_keyword"]
    assert len(scan_lines) == 6 and all(e["passes"] == 2 and e["scrolls"] >= 4 for e in scan_lines), scan_lines
    assert [e["keyword"] for e in scan_lines] == [kw for kw in rec.scan_order]

    # scan -> register -> triage -> capture_links, per keyword (registration precedes the first triage call)
    by_kw: dict[str, list[str]] = {}
    current = None
    for ev in rec.events:
        if ev[0] == "scan":
            current = ev[1]
            by_kw[current] = []
        else:
            by_kw[current].append(ev[0])
    assert list(by_kw) == rec.scan_order
    for kw, kinds in by_kw.items():
        assert "triage" in kinds, kw
        assert kinds == sorted(kinds, key=("triage", "capture").index), (kw, kinds[:5])
        assert kinds.count("capture") <= 1

    counts1 = _counts()
    truth = genuine_authors(rec.scanned_author)
    genuine = len(truth)
    assert counts1["enquiries"] == genuine == r1.posts_relevant > 0
    assert counts1["seen"] == r1.posts_new and r1.posts_new + r1.posts_dupe == r1.posts_seen
    assert llm.calls == counts1["llm_triage"] == r1.posts_new, "each new post is triaged exactly once"
    assert r1.posts_relevant + r1.posts_skipped_ai + r1.triage_failed == r1.posts_new
    assert (r1.current_step or "").startswith(f"Run finished — scanned {r1.posts_seen} · new {r1.posts_new}")

    with session_scope() as db:
        # store only relevant: every enquiry's seen_post is a genuine bulk post; discards have none
        for sp in db.scalars(select(SeenPost)):
            if sp.verdict == "discard":
                assert sp.enquiry_id is None and sp.raw_text and sp.snippet and sp.post_url is None
                assert expected_verdict(sp.raw_text) == "discard"
            else:
                assert sp.verdict == "genuine" and sp.enquiry_id is not None and sp.raw_text is None
        enquiries = list(db.scalars(select(Enquiry)))
        assert {sp.author_name for sp in db.scalars(select(SeenPost)) if sp.enquiry_id} == truth
        assert all(e.ai_verdict == "genuine" for e in enquiries)

        # permalinks only for genuine posts, on both variants (gate assertion 7)
        genuine_ids = {e.id for e in enquiries}
        first_kw = {sp.enquiry_id: sp.keyword_text.lower() for sp in db.scalars(select(SeenPost)) if sp.enquiry_id}
        post_links = {e.id: e for e in enquiries if "/feed/update/urn:li:activity:" in (e.source_url or "")}
        assert r1.permalinks_found == len(post_links) > 0
        for variant in ("sdui", "listitem"):
            linked = [eid for eid in post_links if VARIANTS[first_kw[eid]] == variant]
            assert linked, f"no post link captured on the {variant} variant"
        profile_only = [e for e in enquiries if e.id not in post_links]
        assert all(e.posted_relative for e in profile_only), "a no-op menu keeps the profile link and posted_relative"
        assert genuine_ids == set(first_kw)
    assert rec.menu_clicks == {"Copy link to post"}
    assert rec.menu_opens == sum(rec.capture_attempts) <= genuine <= r1.posts_new
    captured_refs = [ref for ev in rec.events if ev[0] == "capture" for ref in ev[2]]
    assert captured_refs and all(expected_verdict(rec.scanned_text[ref]) == "genuine" for ref in captured_refs)
    assert all(a <= depth.scrape_permalink_max_per_keyword for a in rec.capture_attempts)

    profiles1 = _profiles()
    assert len(profiles1) == counts1["profiles"] > 0
    assert sum(v[5] for v in profiles1.values()) == r1.posts_seen, "one profile row per author, +1 posts_seen per sighting"
    assert sum(v[6] for v in profiles1.values()) == r1.posts_relevant

    # ------------------------------------------------------------------ run 2 (same fixture)
    calls_before, events_before = llm.calls, len(rec.events)
    rid2 = run_discovery(rec, llm)
    with session_scope() as db:
        r2 = runs.get_run(db, rid2).run
    assert r2.status == "succeeded"
    assert r2.posts_seen == r1.posts_seen
    assert (r2.posts_new, r2.posts_relevant, r2.posts_skipped_ai, r2.enquiries_triaged) == (0, 0, 0, 0)
    assert r2.posts_dupe == r2.posts_seen
    assert _counts() == counts1, "enquiries / seen_posts / profiles / triage llm_calls unchanged"
    assert llm.calls == calls_before, "0 new triage calls"
    assert ("triage",) not in rec.events[events_before:]
    assert (r2.current_step or "").startswith(f"Run finished — scanned {r2.posts_seen} · new 0 · relevant 0")

    profiles2 = _profiles()
    assert set(profiles2) == set(profiles1)
    for pid, before in profiles1.items():
        after = profiles2[pid]
        assert (after[0], after[1], after[2], after[3], after[6]) == (before[0], before[1], before[2], before[3], before[6])
        assert after[4] >= before[4] and after[5] > before[5]  # only posts_seen / last_seen_at move
    assert sum(v[5] for v in profiles2.values()) == r1.posts_seen + r2.posts_seen
    # a seen genuine post still lacking a post link is retried; a linked one never reopens its menu
    with session_scope() as db:
        assert all(sp.times_seen >= 2 for sp in db.scalars(select(SeenPost)))

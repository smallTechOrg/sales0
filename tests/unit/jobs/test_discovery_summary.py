"""The run summary is an honest funnel (scanned vs relevant), never "found N enquiries"."""

from __future__ import annotations

from sales0.db.models import ScrapeRun
from sales0.jobs.discovery_job import relevance_pct, summary_text


def run(**counts: int) -> ScrapeRun:
    return ScrapeRun(mode="live", **counts)


def test_summary_states_scanned_new_relevant_skipped_and_seen() -> None:
    r = run(posts_seen=312, posts_new=140, posts_relevant=9, posts_skipped_ai=131, posts_dupe=172)
    assert summary_text(r) == "scanned 312 · new 140 · relevant 9 (6%) · skipped by AI 131 · already seen 172"
    assert relevance_pct(r) == 6.4


def test_summary_without_new_posts_has_no_percentage() -> None:
    r = run(posts_seen=5, posts_new=0, posts_relevant=0, posts_skipped_ai=0, posts_dupe=5)
    assert summary_text(r) == "scanned 5 · new 0 · relevant 0 · skipped by AI 0 · already seen 5"
    assert relevance_pct(r) is None


def test_summary_of_an_empty_run_is_all_zero() -> None:
    assert summary_text(run(posts_seen=0, posts_new=0, posts_relevant=0, posts_skipped_ai=0, posts_dupe=0)).startswith(
        "scanned 0 · new 0 · relevant 0 ·")

"""Prompt regression: real Haiku over the labelled synthetic eval set (agent.md -> Eval).

Gate: verdict accuracy >= 90 %, zero hallucinated emails/phones, every reason <= 160 chars.
"""

from __future__ import annotations

import re

import pytest

from sales0.agent.batch import BatchItem, triage_concurrently
from sales0.agent.schemas import TriageInput, TriageOutcome
from tests.integration.agent.conftest import real_deps
from tests.integration.agent.eval_set import EVAL_POSTS

pytestmark = pytest.mark.llm

MIN_ACCURACY = 0.90


@pytest.fixture(scope="module")
def outcomes() -> dict[str, TriageOutcome]:
    items = [
        BatchItem(
            key=i,
            inp=TriageInput(enquiry_id=1000 + i, text=p.text, author_name=p.author, author_headline=p.headline),
        )
        for i, p in enumerate(EVAL_POSTS)
    ]
    out: dict[str, TriageOutcome] = {}
    for res in triage_concurrently(items, real_deps(), concurrency=4):
        assert res.fatal is None, res.fatal
        assert res.outcome is not None
        out[EVAL_POSTS[res.key].id] = res.outcome
    return out


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def test_eval_set_shape() -> None:
    assert len(EVAL_POSTS) >= 40
    assert sum(p.expected == "genuine" for p in EVAL_POSTS) == 16
    assert sum(p.expected == "discard" for p in EVAL_POSTS) == 24


def test_every_eval_post_is_triaged_and_well_formed(outcomes: dict[str, TriageOutcome]) -> None:
    assert set(outcomes) == {p.id for p in EVAL_POSTS}
    for pid, o in outcomes.items():
        assert o.status == "done" and o.result is not None, (pid, o.error)
        assert 1 <= len(o.result.reason) <= 160
        assert o.prompt_version == "triage-v2"
        assert all(r.cost_usd_micros > 0 for r in o.llm_responses)


def test_verdict_accuracy_is_at_least_90_percent(outcomes: dict[str, TriageOutcome]) -> None:
    wrong = [
        (p.id, p.expected, outcomes[p.id].result.verdict, outcomes[p.id].result.reason)  # type: ignore[union-attr]
        for p in EVAL_POSTS
        if outcomes[p.id].result is None or outcomes[p.id].result.verdict != p.expected  # type: ignore[union-attr]
    ]
    accuracy = 1 - len(wrong) / len(EVAL_POSTS)
    print(f"\nEVAL accuracy={accuracy:.3f} ({len(EVAL_POSTS) - len(wrong)}/{len(EVAL_POSTS)}) wrong={wrong}")
    assert accuracy >= MIN_ACCURACY, f"accuracy {accuracy:.2%}; wrong: {wrong}"


def test_no_hallucinated_emails_or_phones(outcomes: dict[str, TriageOutcome]) -> None:
    for p in EVAL_POSTS:
        r = outcomes[p.id].result
        assert r is not None
        if r.email is not None:
            assert r.email.lower() in p.text.lower(), (p.id, r.email)
        if r.phone is not None:
            assert _digits(r.phone) in _digits(p.text), (p.id, r.phone)


def test_discards_are_low_priority_and_genuine_posts_carry_their_literal_email(
    outcomes: dict[str, TriageOutcome],
) -> None:
    for p in EVAL_POSTS:
        r = outcomes[p.id].result
        assert r is not None
        if r.verdict == "discard":
            assert r.priority == "low"
    g01 = outcomes["G01"].result
    assert g01 is not None and g01.verdict == "genuine"
    assert g01.email == "learning@zenithtextiles.example"

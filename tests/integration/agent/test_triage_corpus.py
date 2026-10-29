"""Real Haiku on the fixture corpus: the expected-outcome table (ai-triage.md -> Success Criteria).

Asserts structure and the stable facts only, never prose. One real call per post.
"""

from __future__ import annotations

import json

import pytest

from tests.integration.agent.conftest import assert_well_formed, corpus_outcome
from tests.integration.agent.corpus import CORPUS, OFFERING_ID, POSTS_JSON, TRIAGED_IDS

llm = pytest.mark.llm


def test_corpus_is_the_exact_discovery_fixture_text_without_the_p7_duplicate() -> None:
    posts = {p["id"]: p for p in json.loads(POSTS_JSON.read_text(encoding="utf-8"))}
    assert set(CORPUS) == set(TRIAGED_IDS) and "P7" not in CORPUS
    for pid, inp in CORPUS.items():
        assert inp.text == posts[pid]["text"]
        assert inp.author_name == posts[pid]["author_name"]
        assert inp.author_headline == posts[pid]["author_headline"]
        assert inp.source_url is not None and posts[pid]["urn"] in inp.source_url


@llm
@pytest.mark.parametrize("post_id", sorted(CORPUS))
def test_every_corpus_post_triages_to_a_well_formed_cold_start_result(post_id: str) -> None:
    outcome = corpus_outcome(post_id)
    assert_well_formed(outcome)
    assert outcome.fewshot_count == 0
    r = outcome.result
    assert r is not None
    # Anti-hallucination on real output: any surviving contact detail is literally in the post.
    text = CORPUS[post_id].text
    if r.email is not None:
        assert r.email in text.lower()
    assert r.offering_id is None or r.offering_id in OFFERING_ID.values()


@llm
def test_p1_acme_is_a_high_priority_offline_pune_buyer_with_the_posted_email() -> None:
    r = corpus_outcome("P1").result
    assert r is not None
    assert r.verdict == "genuine"
    assert r.poster_type == "buyer_company"
    assert r.email == "training@acmelogistics.example"
    assert r.company is not None and "acme" in r.company.lower()
    assert r.mode == "offline"
    assert r.city is not None and "pune" in r.city.lower()
    assert r.offering_id in {OFFERING_ID["AI corporate workshops"], OFFERING_ID["Technical AI training"]}
    assert r.priority == "high"
    # The phone survives the guard only because its digits are in the post.
    assert r.phone is None or "9820011111" in "".join(ch for ch in r.phone if ch.isdigit())


@llm
def test_p2_agency_hiring_freelance_trainers_is_discarded() -> None:
    r = corpus_outcome("P2").result
    assert r is not None
    assert r.verdict == "discard" and r.priority == "low"
    assert r.poster_type in {"training_agency", "individual_trainer_seeker"}


@llm
def test_p3_individual_trainer_at_hourly_rate_is_discarded() -> None:
    r = corpus_outcome("P3").result
    assert r is not None
    assert r.verdict == "discard" and r.priority == "low"
    assert r.poster_type in {"individual_trainer_seeker", "training_agency"}


@llm
def test_p5_hindi_emoji_post_is_genuine_with_email_city_and_summary() -> None:
    r = corpus_outcome("P5").result
    assert r is not None
    assert r.verdict == "genuine"
    assert r.email == "arjun.rao@zentrix.example"
    assert r.city is not None and ("bengaluru" in r.city.lower() or "bangalore" in r.city.lower())
    assert r.requirement_summary.strip()


@llm
def test_p6_online_agent_workshop_is_genuine_with_no_invented_contact() -> None:
    r = corpus_outcome("P6").result
    assert r is not None
    assert r.verdict == "genuine"
    assert r.email is None and r.phone is None
    assert r.mode == "online"
    assert r.offering_id == OFFERING_ID["Technical AI training"]


@llm
def test_p8_build_an_ai_assistant_matches_consulting_and_build() -> None:
    r = corpus_outcome("P8").result
    assert r is not None
    assert r.verdict == "genuine"
    assert r.offering_id == OFFERING_ID["AI consulting & build services"]
    assert r.email == "it-projects@meridian.example"


@llm
def test_p9_trainer_open_to_work_is_discarded() -> None:
    r = corpus_outcome("P9").result
    assert r is not None
    assert r.verdict == "discard" and r.priority == "low"
    assert r.poster_type in {"job_seeker_or_self_promo", "other"}


@llm
def test_p10_claude_code_training_matches_technical_ai_training() -> None:
    r = corpus_outcome("P10").result
    assert r is not None
    assert r.verdict == "genuine"
    assert r.offering_id == OFFERING_ID["Technical AI training"]


@llm
def test_p11_academy_onboarding_freelance_trainers_is_discarded() -> None:
    r = corpus_outcome("P11").result
    assert r is not None
    assert r.verdict == "discard" and r.priority == "low"

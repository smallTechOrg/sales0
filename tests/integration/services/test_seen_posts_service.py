"""services.seen_posts on real SQLite: sighting/dedupe, persistence of triage, permalinks, skipped list,
rescue/override, retry (real Haiku) and the 14-day purge."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.agent import TriageOutcome, TriageResult
from sales0.clock import utcnow
from sales0.db.engine import session_scope
from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    EnquirySource,
    LinkedinProfile,
    LlmCall,
    SeenPost,
    StatusHistory,
    TriageDecision,
)
from sales0.errors import DomainError
from sales0.services import inbox, profiles, seen_posts
from sales0.services.seen_posts import (
    attach_permalink,
    list_skipped,
    persist_triage,
    purge_expired,
    register_sighting,
    rescue,
    triage_inputs,
)
from tests.integration.services.helpers import make_run, sdui_post
from tests.support.fake_llm import FakeLLMClient, make_response, triage_answer

BUYER = "Acme Logistics wants a training partner for a 2-day GenAI workshop in Pune. Mail training@acme.example"
AGENCY = "SkillBridge is hiring freelance Generative AI trainers. Send your profile to trainers@skillbridge.example"
KW = "corporate ai training"


def outcome(verdict: str = "genuine", **overrides: object) -> TriageOutcome:
    if verdict == "discard":
        overrides.setdefault("poster_type", "training_agency")
    answer = triage_answer(verdict=verdict, **overrides)
    return TriageOutcome(
        status="done",
        result=TriageResult.model_validate(answer),
        error=None,
        llm_responses=[make_response(answer)],
        fewshot_count=0,
        prompt_version="triage-v4",
    )


def failed_outcome() -> TriageOutcome:
    return TriageOutcome(status="failed", result=None, error="bad JSON", llm_responses=[], fewshot_count=0,
                         prompt_version="triage-v4")


def sight(db: Session, key: str, text: str, run_id: int, **kw: object):
    return register_sighting(db, sdui_post(key, text, **kw), run_id, None, KW)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- register_sighting
def test_new_post_is_stored_pending_with_text_profile_and_purge_date(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k1", BUYER, run_id)
    assert (s.kind, s.verdict, s.triage_status, s.enquiry_id, s.needs_permalink) == ("new", None, "pending", None, False)
    row = db.get(SeenPost, s.seen_post_id)
    assert row is not None and row.raw_text == BUYER and row.source_ref == "ck:k1"
    assert row.times_seen == 1 and row.keyword_text == KW and row.first_run_id == run_id
    assert row.snippet == BUYER and row.text_prefix == seen_posts.normalize_text(BUYER)[:300]
    assert row.raw_purge_after is not None and row.raw_purge_after - row.first_seen_at == timedelta(days=14)
    assert row.post_url is None and row.posted_relative == "2d"
    profile = db.get(LinkedinProfile, s.profile_id)
    assert profile is not None and (profile.slug, profile.posts_seen) == ("in/priya-menon-fx", 1)
    assert db.scalar(select(func.count(Enquiry.id))) == 0  # nothing becomes an enquiry before triage


def test_same_ref_same_text_and_same_content_other_author_are_seen_not_duplicated(db: Session) -> None:
    run_id = make_run()
    first = sight(db, "k1", BUYER, run_id)
    by_ref = sight(db, "k1", "completely different text now", run_id)
    by_fingerprint = sight(db, "k2", BUYER.upper() + " #hiring", run_id)
    by_content = sight(db, "k3", BUYER, run_id, slug="in/someone-else", author="Someone")
    assert [x.kind for x in (by_ref, by_fingerprint, by_content)] == ["seen"] * 3
    assert {x.seen_post_id for x in (by_ref, by_fingerprint, by_content)} == {first.seen_post_id}
    row = db.get(SeenPost, first.seen_post_id)
    assert row is not None and row.times_seen == 4
    assert db.scalar(select(func.count(SeenPost.id))) == 1
    # every sighting touches the author profile, repeats included
    assert db.scalar(select(LinkedinProfile.posts_seen).where(LinkedinProfile.slug == "in/priya-menon-fx")) == 3


def test_truncated_then_expanded_text_from_the_same_author_is_a_near_duplicate(db: Session) -> None:
    run_id = make_run()
    long_text = (BUYER + " ") * 3
    first = sight(db, "k1", long_text[:200] + "… more", run_id)
    again = sight(db, "k9", long_text, run_id)
    assert again.kind == "seen" and again.seen_post_id == first.seen_post_id
    other_author = sight(db, "k10", long_text, run_id, slug="in/other-person", author="Other")
    assert other_author.kind == "new"  # a different author's expanded text is its own post
    assert db.scalar(select(func.count(SeenPost.id))) == 2


def test_short_texts_from_one_author_are_not_merged_by_prefix(db: Session) -> None:
    run_id = make_run()
    a = sight(db, "k1", "Need a trainer for GenAI", run_id)
    b = sight(db, "k2", "Need a trainer for Claude", run_id)
    assert (a.kind, b.kind) == ("new", "new") and a.seen_post_id != b.seen_post_id


def test_post_without_author_profile_and_empty_text_do_not_collapse(db: Session) -> None:
    run_id = make_run()
    a = sight(db, "e1", "", run_id, slug="")
    b = sight(db, "e2", "", run_id, slug="")
    c = sight(db, "n1", BUYER, run_id, slug="")
    assert (a.kind, b.kind, c.kind) == ("new", "new", "new") and a.profile_id is None
    assert db.scalar(select(func.count(LinkedinProfile.id))) == 0


def test_triage_inputs_use_the_seen_post_id_and_skip_purged_text(db: Session) -> None:
    run_id = make_run()
    a, b = sight(db, "k1", BUYER, run_id), sight(db, "k2", AGENCY, run_id, slug="in/skillbridge")
    db.get(SeenPost, b.seen_post_id).raw_text = None  # type: ignore[union-attr]
    pairs = triage_inputs(db, [a.seen_post_id, b.seen_post_id, a.seen_post_id])
    assert [p[0] for p in pairs] == [a.seen_post_id]
    inp = pairs[0][1]
    assert inp.enquiry_id == a.seen_post_id and inp.text == BUYER
    assert inp.author_name == "Priya Menon" and inp.source_url == "https://www.linkedin.com/in/priya-menon-fx/"
    assert triage_inputs(db, []) == []


# ----------------------------------------------------------------------------- persist_triage
def test_genuine_creates_the_enquiry_and_moves_the_text(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k1", BUYER, run_id)
    out = outcome("genuine", company="Acme Logistics", email="training@acme.example")
    p = persist_triage(db, s.seen_post_id, out, run_id=run_id, keyword_id=None)
    assert (p.verdict, p.triage_status) == ("genuine", "done") and p.enquiry_id is not None
    assert p.cost_usd_micros == out.llm_responses[0].cost_usd_micros > 0
    row = db.get(SeenPost, s.seen_post_id)
    e = db.get(Enquiry, p.enquiry_id)
    assert row is not None and e is not None
    assert (row.raw_text, row.raw_purge_after, row.enquiry_id, row.verdict) == (None, None, e.id, "genuine")
    assert row.snippet == BUYER and row.ai_result is not None
    assert (e.raw_text, e.review_status, e.triage_status, e.ai_verdict, e.priority) == (
        BUYER, "pending_review", "done", "genuine", "high")
    assert (e.seen_post_id, e.author_profile_id, e.posted_relative, e.prompt_version) == (
        row.id, s.profile_id, "2d", "triage-v4")
    assert e.company == "Acme Logistics" and e.content_hash == row.content_hash and e.date_posted is not None
    assert e.source_url == "https://www.linkedin.com/in/priya-menon-fx/"  # no permalink yet: the profile
    assert [x.source_ref for x in e.sources] == ["ck:k1"] and [h.keyword_text for h in e.keyword_hits] == [KW]
    assert db.scalar(select(LinkedinProfile.relevant_count).where(LinkedinProfile.id == s.profile_id)) == 1
    call = db.scalars(select(LlmCall).where(LlmCall.seen_post_id == row.id)).one()
    assert (call.enquiry_id, call.run_id, call.feature, call.status) == (e.id, run_id, "triage", "ok")
    fields = {h.field for h in db.scalars(select(StatusHistory).where(StatusHistory.entity_id == e.id))}
    assert {"triage_status", "review_status"} <= fields


def test_discard_keeps_the_text_and_creates_no_enquiry(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k2", AGENCY, run_id)
    p = persist_triage(db, s.seen_post_id, outcome("discard", reason="Agency recruiting freelancers"),
                       run_id=run_id, keyword_id=None)
    assert (p.verdict, p.enquiry_id, p.triage_status) == ("discard", None, "done")
    row = db.get(SeenPost, s.seen_post_id)
    assert row is not None and (row.raw_text, row.reason, row.poster_type) == (AGENCY, "Agency recruiting freelancers", "training_agency")
    assert row.raw_purge_after is not None
    assert db.scalar(select(func.count(Enquiry.id))) == 0
    assert db.scalar(select(LinkedinProfile.relevant_count)) == 0


def test_failed_and_missing_outcomes_mark_failed_with_an_error_row(db: Session) -> None:
    run_id = make_run()
    a, b = sight(db, "k1", BUYER, run_id), sight(db, "k2", AGENCY, run_id, slug="in/sb")
    pa = persist_triage(db, a.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
    pb = persist_triage(db, b.seen_post_id, None, error="LLM unavailable", run_id=run_id, keyword_id=None)
    assert (pa.triage_status, pb.triage_status, pa.verdict, pa.enquiry_id) == ("failed", "failed", None, None)
    ra, rb = db.get(SeenPost, a.seen_post_id), db.get(SeenPost, b.seen_post_id)
    assert ra is not None and rb is not None
    assert (ra.triage_error, rb.triage_error) == ("bad JSON", "LLM unavailable") and ra.raw_text == BUYER
    assert db.scalars(select(LlmCall.status).where(LlmCall.seen_post_id == b.seen_post_id)).all() == ["error"]
    assert db.scalar(select(func.count(Enquiry.id))) == 0
    assert db.scalar(select(func.count(SeenPost.id))) == 2


def test_persist_unknown_seen_post_is_not_found(db: Session) -> None:
    with pytest.raises(DomainError) as info:
        persist_triage(db, 999, outcome(), run_id=None, keyword_id=None)
    assert info.value.code == "not_found"


def test_a_genuine_post_seen_again_merges_keyword_ref_and_permalink_without_a_new_enquiry(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k1", BUYER, run_id)
    p = persist_triage(db, s.seen_post_id, outcome(), run_id=run_id, keyword_id=None)
    urn = "urn:li:activity:7300000000000000077"
    again = register_sighting(
        db, sdui_post("k5", BUYER, urn=urn), run_id, None, "ai workshop")
    assert again.kind == "seen" and again.enquiry_id == p.enquiry_id and again.verdict == "genuine"
    assert again.needs_permalink is False  # the sighting itself carried the permalink -> upgraded
    e = db.get(Enquiry, p.enquiry_id)
    assert e is not None and e.source_url.endswith(f"{urn}/")
    assert sorted(x.source_ref for x in e.sources) == sorted(["ck:k1", urn])
    assert sorted(h.keyword_text for h in e.keyword_hits) == ["ai workshop", KW]
    assert db.scalar(select(func.count(Enquiry.id))) == 1


def test_needs_permalink_for_a_genuine_post_without_a_post_link(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k1", BUYER, run_id)
    persist_triage(db, s.seen_post_id, outcome(), run_id=run_id, keyword_id=None)
    again = sight(db, "k1", BUYER, run_id)
    assert again.kind == "seen" and again.needs_permalink is True and again.triage_status == "done"


def test_pending_seen_post_is_reported_pending_when_sighted_again(db: Session) -> None:
    run_id = make_run()
    sight(db, "k1", BUYER, run_id)
    again = sight(db, "k1", BUYER, run_id)
    assert (again.kind, again.triage_status, again.verdict) == ("seen", "pending", None)


# ----------------------------------------------------------------------------- attach_permalink
def test_attach_permalink_sets_post_url_upgrades_enquiry_and_adds_urn_source(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k1", BUYER, run_id)
    p = persist_triage(db, s.seen_post_id, outcome(), run_id=run_id, keyword_id=None)
    urn = "urn:li:activity:7300000000000000088"
    url = f"https://www.linkedin.com/feed/update/{urn}/"
    assert attach_permalink(db, s.seen_post_id, url, urn=urn) is True
    row, e = db.get(SeenPost, s.seen_post_id), db.get(Enquiry, p.enquiry_id)
    assert row is not None and e is not None and row.post_url == url and e.source_url == url
    assert db.scalar(select(EnquirySource.id).where(EnquirySource.source_ref == urn)) is not None
    assert attach_permalink(db, s.seen_post_id, url, urn=urn) is False  # idempotent
    other = "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000099/"
    attach_permalink(db, s.seen_post_id, other)
    assert db.get(Enquiry, p.enquiry_id).source_url == url  # type: ignore[union-attr]  # a post link is never replaced


def test_attach_permalink_on_a_skipped_post_only_records_post_url(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k2", AGENCY, run_id)
    persist_triage(db, s.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    url = "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000055/"
    assert attach_permalink(db, s.seen_post_id, url) is True
    assert db.get(SeenPost, s.seen_post_id).post_url == url  # type: ignore[union-attr]
    with pytest.raises(DomainError):
        attach_permalink(db, 12345, url)


# ----------------------------------------------------------------------------- skipped list
def _skipped_fixture(db: Session) -> dict[str, int]:
    run_id = make_run()
    ids = {}
    for name, key, text, slug, verdict in (
        ("agency", "a", AGENCY, "in/skillbridge", "discard"),
        ("trainer", "b", "I am a certified GenAI trainer, hire me for freelance work", "in/trainer-t", "discard"),
        ("buyer", "c", BUYER, "in/priya", "genuine"),
        ("fail", "d", "Something unreadable by the AI at all", "in/failer", "fail"),
        ("pending", "e", "Not triaged yet but scanned", "in/pend", None),
    ):
        s = sight(db, key, text, run_id, slug=slug, author=name.title())
        ids[name] = s.seen_post_id
        if verdict == "fail":
            persist_triage(db, s.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
        elif verdict:
            persist_triage(db, s.seen_post_id, outcome(verdict, reason=f"{name} reason"), run_id=run_id, keyword_id=None)
    return ids


def test_list_skipped_shows_only_ai_discards_with_counts_and_newest_first(db: Session) -> None:
    ids = _skipped_fixture(db)
    page = list_skipped(db)
    assert [i.id for i in page.items] == [ids["trainer"], ids["agency"]]
    assert (page.total, page.state, page.counts) == (2, "discard", {"discard": 2, "failed": 1})
    item = page.items[1]
    assert item.snippet == AGENCY and item.reason == "agency reason" and item.can_rescue is True
    assert (item.author_name, item.profile_url, item.times_seen, item.keyword_text) == (
        "Agency", "https://www.linkedin.com/in/skillbridge/", 1, KW)
    assert item.link_kind == "profile" and item.raw_text == AGENCY


def test_list_skipped_search_is_case_insensitive_over_snippet_reason_author_and_text(db: Session) -> None:
    ids = _skipped_fixture(db)
    assert [i.id for i in list_skipped(db, q="FREELANCE").items] == [ids["trainer"], ids["agency"]]
    assert [i.id for i in list_skipped(db, q="trainer reason").items] == [ids["trainer"]]
    assert [i.id for i in list_skipped(db, q="agency").items] == [ids["agency"]]
    assert list_skipped(db, q="zzzz").items == [] and list_skipped(db, q="zzzz").total == 0
    assert list_skipped(db, q="100%").total == 0  # LIKE wildcards are escaped
    # full text is searched only while retained; the snippet keeps matching afterwards
    db.get(SeenPost, ids["agency"]).raw_text = None  # type: ignore[union-attr]
    assert ids["agency"] in [i.id for i in list_skipped(db, q="skillbridge").items]


def test_list_skipped_failed_state_and_paging(db: Session) -> None:
    ids = _skipped_fixture(db)
    failed = list_skipped(db, state="failed")
    assert [i.id for i in failed.items] == [ids["fail"]]
    assert failed.items[0].triage_error == "bad JSON" and failed.items[0].can_rescue is False
    p1 = list_skipped(db, page_size=1, page=1)
    p2 = list_skipped(db, page_size=1, page=2)
    assert (p1.total, p1.has_next, p2.has_next, p2.has_prev) == (2, True, False, True)
    assert p1.items[0].id != p2.items[0].id
    assert list_skipped(db, page=0, page_size=0).page == 1


def test_inbox_tab_counts_include_skipped_and_skipped_failed(db: Session) -> None:
    _skipped_fixture(db)
    counts = inbox.tab_counts(db)
    assert counts["skipped"] == 2 and counts["skipped_failed"] == 1 and counts["review"] == 1


# ----------------------------------------------------------------------------- rescue / override
def test_rescue_creates_a_review_enquiry_keeps_the_ai_verdict_and_leaves_the_list(db: Session) -> None:
    ids = _skipped_fixture(db)
    card = rescue(db, ids["agency"])
    assert (card.review_status, card.ai_verdict, card.triage_status, card.raw_text) == (
        "pending_review", "discard", "done", AGENCY)
    assert card.ai_reason == "agency reason" and card.stage is None
    row = db.get(SeenPost, ids["agency"])
    assert row is not None and row.enquiry_id == card.id and row.rescued_at is not None and row.raw_text is None
    assert ids["agency"] not in [i.id for i in list_skipped(db).items]
    assert inbox.tab_counts(db)["skipped"] == 1 and inbox.tab_counts(db)["review"] == 2
    assert db.scalar(select(LinkedinProfile.relevant_count).where(LinkedinProfile.slug == "in/skillbridge")) == 1
    assert db.scalar(select(func.count(TriageDecision.id))) == 0
    with pytest.raises(DomainError) as info:
        rescue(db, ids["agency"])
    assert (info.value.code, info.value.http_status) == ("already_rescued", 409)


def test_override_accepts_and_records_an_override_decision(db: Session) -> None:
    ids = _skipped_fixture(db)
    card = rescue(db, ids["trainer"], accept=True, actor_user_id=None)
    assert (card.review_status, card.stage, card.is_override) == ("accepted", "prospect_new", True)
    d = db.scalars(select(TriageDecision).where(TriageDecision.enquiry_id == card.id)).one()
    assert (d.decision, d.ai_verdict, d.is_override) == ("accept", "discard", True)


def test_rescue_errors_not_found_purged_and_no_ai_result(db: Session) -> None:
    ids = _skipped_fixture(db)
    with pytest.raises(DomainError) as nf:
        rescue(db, 9999)
    assert nf.value.code == "not_found"
    with pytest.raises(DomainError) as nr:
        rescue(db, ids["fail"])
    assert nr.value.code == "triage_failed"
    purge_expired(db, now=utcnow() + timedelta(days=15))
    with pytest.raises(DomainError) as purged:
        rescue(db, ids["agency"])
    assert (purged.value.code, purged.value.http_status) == ("raw_purged", 410)


# ----------------------------------------------------------------------------- purge
def test_purge_expired_drops_only_expired_text_and_keeps_fingerprint_and_snippet(db: Session) -> None:
    ids = _skipped_fixture(db)
    before = db.get(SeenPost, ids["agency"])
    assert before is not None
    fp, prefix, snippet = before.fingerprint, before.text_prefix, before.snippet
    now = utcnow()
    assert purge_expired(db, now=now + timedelta(days=13)) == 0
    # only the two triaged discards are purged (fixscrape #6: pending/failed rows keep their text so
    # they can still be triaged/retried); the genuine one's text already moved to its enquiry
    assert purge_expired(db, now=now + timedelta(days=15)) == 2
    row = db.get(SeenPost, ids["agency"])
    assert row is not None and row.raw_text is None and row.raw_purged_at is not None
    assert (row.fingerprint, row.text_prefix, row.snippet) == (fp, prefix, snippet)
    assert list_skipped(db).items[0].can_rescue is False
    assert purge_expired(db, now=now + timedelta(days=30)) == 0  # idempotent
    assert db.scalar(select(func.count(Enquiry.id))) == 1  # the genuine enquiry keeps its full text
    assert db.scalar(select(Enquiry.raw_text)) == BUYER


def test_a_purged_post_found_again_makes_no_new_row(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k2", AGENCY, run_id)
    persist_triage(db, s.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
    purge_expired(db, now=utcnow() + timedelta(days=15))
    again = sight(db, "k2", AGENCY, run_id)
    assert (again.kind, again.seen_post_id, again.verdict) == ("seen", s.seen_post_id, "discard")
    assert db.scalar(select(func.count(SeenPost.id))) == 1


def test_profile_counters_survive_a_two_pass_flow(db: Session) -> None:
    run_id = make_run()
    s = sight(db, "k1", BUYER, run_id)
    persist_triage(db, s.seen_post_id, outcome(), run_id=run_id, keyword_id=None)
    sight(db, "k1", BUYER, run_id)
    view = profiles.get_profile(db, "in/priya-menon-fx")
    assert view is not None and (view.posts_seen, view.relevant_count) == (2, 1)
    assert db.scalar(select(func.count(EnquiryKeywordHit.id))) == 1


# ----------------------------------------------------------------------------- retry_triage (real Haiku)
@pytest.mark.llm
def test_retry_triage_makes_one_real_call_and_a_genuine_result_becomes_an_enquiry(db_url: str) -> None:
    run_id = make_run()
    with session_scope() as db:
        s = sight(db, "k1", BUYER, run_id)
        persist_triage(db, s.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
        sid = s.seen_post_id
    with session_scope() as db:
        view = seen_posts.retry_triage(db, sid)
    assert view.triage_status == "done"
    with session_scope() as db:
        row = db.get(SeenPost, sid)
        assert row is not None and row.verdict in ("genuine", "discard") and row.reason
        ok = db.scalars(select(LlmCall).where(LlmCall.seen_post_id == sid, LlmCall.status == "ok")).all()
        assert len(ok) >= 1 and all(c.cost_usd_micros > 0 for c in ok)
        if row.verdict == "genuine":
            assert row.enquiry_id is not None and row.raw_text is None
            enquiry = db.get(Enquiry, row.enquiry_id)
            assert enquiry is not None and enquiry.raw_text == BUYER and enquiry.email in (None, "training@acme.example")
        else:
            assert row.enquiry_id is None and row.raw_text == BUYER


def test_retry_triage_with_a_failing_llm_keeps_it_failed_and_raises(db_url: str) -> None:
    from sales0.llm import LLMTransientError

    run_id = make_run()
    with session_scope() as db:
        s = sight(db, "k1", BUYER, run_id)
        persist_triage(db, s.seen_post_id, failed_outcome(), run_id=run_id, keyword_id=None)
        sid = s.seen_post_id
    fake = FakeLLMClient([LLMTransientError("boom"), LLMTransientError("boom"), LLMTransientError("boom")])
    with session_scope() as db, pytest.raises(DomainError) as info:
        seen_posts.retry_triage(db, sid, fake)
    assert info.value.code == "triage_failed"
    with session_scope() as db:
        row = db.get(SeenPost, sid)
        assert row is not None and row.triage_status == "failed" and row.triage_error
        assert db.scalar(select(func.count(Enquiry.id))) == 0


def test_retry_triage_errors_for_missing_and_purged_posts(db_url: str) -> None:
    run_id = make_run()
    with session_scope() as db:
        with pytest.raises(DomainError) as nf:
            seen_posts.retry_triage(db, 4242)
        assert nf.value.code == "not_found"
        s = sight(db, "k1", BUYER, run_id)
        # a triaged discard (pending/failed rows are no longer purged, fixscrape #6)
        persist_triage(db, s.seen_post_id, outcome("discard"), run_id=run_id, keyword_id=None)
        purge_expired(db, now=utcnow() + timedelta(days=15))
        with pytest.raises(DomainError) as purged:
            seen_posts.retry_triage(db, s.seen_post_id)
        assert purged.value.code == "raw_purged"


@pytest.fixture(autouse=True)
def _purge_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw text is kept forever by default; these tests exercise the opt-in 14-day purge."""
    from sales0.config import reset_settings

    monkeypatch.setenv("SALES0_SEEN_RAW_RETENTION_DAYS", "14")
    reset_settings()

"""Pasted post: dedupe, enquiry in To review, ONE triage call, failure handling, idempotency."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, EnquirySource, ImportBatch, LlmCall, Prospect, SeenPost, TimelineEvent
from sales0.errors import DomainError
from sales0.llm import LLMAuthError, LLMTransientError, reset_llm_client, set_llm_client
from sales0.scraper.types import ScrapedPost
from sales0.services import dedupe, importer, inbox
from sales0.services.crm.actor import Actor
from tests.support.fake_llm import FakeLLMClient, triage_answer

ANN = Actor("Ann Rao")
P1 = (
    "We are looking for a training partner! Acme Logistics wants a training partner to run a 2-day Generative AI "
    "workshop for 40 managers at our Pune office. The sessions will be offline (in person), in the week of 17 Nov. "
    "Please send your proposals to training@acmelogistics.example or call +91 98200 11111."
)
LINK = "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000009901/"
PROFILE = "https://www.linkedin.com/in/priya-menon-paste/"


@pytest.fixture(autouse=True)
def _reset_llm() -> object:
    yield
    reset_llm_client()


def paste(db: Session, llm: object, text: str = P1, **kw: object) -> importer.PasteResult:
    kw.setdefault("link", None)
    kw.setdefault("author", None)
    kw.setdefault("author_profile_url", None)
    res = importer.paste_post(db, llm, text=text, actor=ANN, **kw)  # type: ignore[arg-type]
    db.commit()
    return res


def n(db: Session, model: type) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def test_paste_creates_a_pending_enquiry_with_an_ai_verdict_and_one_triage_call(db: Session) -> None:
    fake = FakeLLMClient([triage_answer(offering_id=None)])
    res = paste(db, fake, link=LINK, author="Priya Menon", author_profile_url=PROFILE)
    assert (res.outcome, res.verdict, res.triage_status) == ("created", "genuine", "done")
    e = db.get(Enquiry, res.enquiry_id)
    assert e is not None
    assert (e.origin, e.review_status, e.source, e.triage_status) == ("manual", "pending_review", "linkedin_post", "done")
    assert e.raw_text == P1 and e.source_url == LINK and e.author_name == "Priya Menon"
    assert e.author_profile_id is not None and e.seen_post_id is not None and e.ai_verdict == "genuine"
    seen = db.get(SeenPost, e.seen_post_id)
    assert seen is not None and seen.enquiry_id == e.id and seen.verdict == "genuine" and seen.raw_text is None
    assert db.scalar(select(func.count()).select_from(EnquirySource).where(EnquirySource.enquiry_id == e.id)) == 1
    assert fake.calls == 1
    assert db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.enquiry_id == e.id)) == 1
    batch = db.scalars(select(ImportBatch)).one()
    assert (batch.kind, batch.rows_new, batch.actor) == ("paste", 1, "Ann Rao")
    assert db.scalar(select(func.count()).select_from(TimelineEvent).where(TimelineEvent.enquiry_id == e.id)) == 1
    page = inbox.list_enquiries(db, "review", None)
    assert [c.id for c in page.items] == [e.id] and page.items[0].ai_verdict == "genuine"


def test_pasting_the_same_post_again_is_a_duplicate_and_creates_nothing(db: Session) -> None:
    fake = FakeLLMClient([triage_answer(offering_id=None)])
    first = paste(db, fake, link=LINK)
    for variant in (P1, "  " + P1.upper() + "  #hiring https://lnkd.in/xyz", P1.replace(". ", ".\n")):
        again = paste(db, fake, variant)
        assert (again.outcome, again.enquiry_id) == ("duplicate", first.enquiry_id)
    by_link = paste(db, fake, "A completely different wording of a post that is long enough to count.", link=LINK)
    assert (by_link.outcome, by_link.enquiry_id) == ("duplicate", first.enquiry_id)
    assert n(db, Enquiry) == 1 and n(db, SeenPost) == 1 and n(db, ImportBatch) == 1
    assert fake.calls == 1  # duplicates are never triaged


def test_a_discard_verdict_still_lands_in_to_review(db: Session) -> None:
    fake = FakeLLMClient([triage_answer(verdict="discard", poster_type="other", priority="low", offering_id=None, reason="Not a buyer")])  # noqa: E501
    res = paste(db, fake)
    e = db.get(Enquiry, res.enquiry_id)
    assert res.verdict == "discard" and e is not None and e.review_status == "pending_review"


@pytest.mark.parametrize("failure", [LLMTransientError("overloaded"), LLMAuthError("bad key")])
def test_a_failed_triage_keeps_the_enquiry_for_a_retry(db: Session, failure: Exception) -> None:
    res = paste(db, FakeLLMClient([failure, failure]))
    assert res.outcome == "created" and res.triage_status == "failed" and res.verdict is None
    e = db.get(Enquiry, res.enquiry_id)
    assert e is not None and e.triage_status == "failed" and e.triage_error and e.review_status == "pending_review"
    seen = db.get(SeenPost, e.seen_post_id)
    assert seen is not None and seen.triage_status == "failed"
    # the normal inbox Retry works on it
    set_llm_client(FakeLLMClient([triage_answer(offering_id=None)]))
    card = inbox.retry_triage(db, e.id)
    db.commit()
    assert card.triage_status == "done" and card.ai_verdict == "genuine"


@pytest.mark.parametrize("text", ["", "   ", "too short", "x" * 39, "\n\n" + "y" * 20 + "\n\n", "#### https://lnkd.in/a/b/c/d/e/f/g/h/i/j/k"])
def test_text_under_40_chars_or_with_no_words_is_refused(db: Session, text: str) -> None:
    fake = FakeLLMClient([triage_answer()])
    with pytest.raises(DomainError) as e:
        importer.paste_post(db, fake, text=text, link=None, author=None, author_profile_url=None, actor=ANN)
    assert (e.value.code, e.value.http_status) == ("paste_invalid", 400)
    assert fake.calls == 0
    db.rollback()
    assert n(db, Enquiry) == 0


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "not a url", "ftp://x.example/p", "https://"])
def test_a_bad_link_is_refused(db: Session, bad: str) -> None:
    with pytest.raises(DomainError) as e:
        importer.paste_post(db, FakeLLMClient([triage_answer()]), text=P1, link=bad, author=None, author_profile_url=None, actor=ANN)  # noqa: E501
    assert e.value.code == "paste_invalid"


def test_a_post_the_ai_skipped_earlier_is_adopted_not_duplicated(db: Session) -> None:
    from sales0.services.seen_posts import fingerprint, snippet_of

    seen = SeenPost(
        fingerprint=fingerprint(None, P1), content_hash=dedupe.content_hash(P1), keyword_text="x", triage_status="done",
        verdict="discard", snippet=snippet_of(P1), raw_text=P1,
    )
    db.add(seen)
    db.commit()
    res = paste(db, FakeLLMClient([triage_answer(offering_id=None)]))
    assert res.outcome == "created"
    db.refresh(seen)
    assert seen.enquiry_id == res.enquiry_id and seen.rescued_at is not None and seen.raw_text is None
    assert n(db, SeenPost) == 1 and paste(db, FakeLLMClient([]), P1).outcome == "duplicate"


def test_discovery_of_the_same_post_later_dedupes_against_the_pasted_enquiry(db: Session) -> None:
    res = paste(db, FakeLLMClient([triage_answer(offering_id=None)]), link=LINK, author_profile_url=PROFILE)
    scraped = ScrapedPost(
        source_ref="urn:li:activity:7300000000000009901", url=LINK, author_name="Priya", author_profile_url=PROFILE,
        author_headline=None, posted_relative="1d", posted_date=None, text=P1 + " ", link_kind="post",
    )
    from sales0.db.models import ScrapeRun

    run = ScrapeRun(trigger="manual", mode="live", status="running")
    db.add(run)
    db.commit()
    kind, eid = dedupe.upsert_post(db, scraped, run.id, None, "kw")
    db.commit()
    assert kind in ("dupe_urn", "dupe_content") and eid == res.enquiry_id and n(db, Enquiry) == 1


def test_accepting_a_pasted_enquiry_is_the_normal_inbox_action(db: Session) -> None:
    from sales0.services import crm, events

    events.reset()
    crm.install()
    res = paste(db, FakeLLMClient([triage_answer(offering_id=None)]), author_profile_url=PROFILE)
    inbox.decide(db, [res.enquiry_id], "accept")
    db.commit()
    e = db.get(Enquiry, res.enquiry_id)
    assert e is not None and e.review_status == "accepted" and e.prospect_id is not None
    assert db.scalar(select(func.count()).select_from(Prospect)) == 1
    inbox.decide(db, [res.enquiry_id], "accept")  # again: still one prospect
    db.commit()
    assert db.scalar(select(func.count()).select_from(Prospect)) == 1


@pytest.mark.llm
def test_real_haiku_triage_of_a_pasted_post(db: Session) -> None:
    res = paste(db, None, link=LINK, author="Priya Menon", author_profile_url=PROFILE)
    assert res.outcome == "created" and res.triage_status == "done" and res.verdict == "genuine"
    e = db.get(Enquiry, res.enquiry_id)
    assert e is not None and e.review_status == "pending_review" and e.ai_reason and e.requirement_summary
    rows = db.scalars(select(LlmCall).where(LlmCall.enquiry_id == e.id)).all()
    assert rows and all(r.status == "ok" and r.input_tokens > 0 and r.cost_usd_micros > 0 for r in rows)
    again = paste(db, None, P1.replace("Please send", "Please do send"), link=LINK)
    assert again.outcome == "duplicate" and again.enquiry_id == e.id
    assert db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.feature == "triage")) == len(rows)

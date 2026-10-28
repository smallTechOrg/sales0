"""ORM models against a migrated SQLite file: normalisation keys, uniqueness, FK delete
rules, enum check constraints, timezone-aware datetimes and dialect-safe filters."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from sales0.db.engine import get_sessionmaker, session_scope
from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    EnquirySource,
    Keyword,
    LinkedInSession,
    LlmCall,
    Offering,
    OfferingGroup,
    ScrapeRun,
    ScrapeRunKeyword,
    StatusHistory,
    TriageDecision,
    User,
)


def _enquiry(**overrides: object) -> Enquiry:
    values: dict[str, object] = {
        "source": "linkedin_post",
        "content_hash": "a" * 64,
        "raw_text": "We are looking for a corporate AI training partner for 40 managers in Pune.",
    }
    values.update(overrides)
    return Enquiry(**values)


def _expect_integrity_error(obj_factory) -> None:  # type: ignore[no-untyped-def]
    db = get_sessionmaker()()
    try:
        obj_factory(db)
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


# --- keywords -------------------------------------------------------------------------


def test_keyword_text_is_trimmed_collapsed_and_keyed_case_insensitively(
    db_url: str,
) -> None:
    with session_scope() as db:
        kw = Keyword(text="  Corporate   AI\ttraining ")
        db.add(kw)
        db.flush()
        assert kw.text == "Corporate AI training"
        assert kw.text_key == "corporate ai training"
        assert kw.active is True and kw.last_run_at is None
        assert kw.created_at.tzinfo is not None


def test_duplicate_keyword_differing_only_in_case_violates_uniqueness(
    db_url: str,
) -> None:
    with session_scope() as db:
        db.add(Keyword(text="AI Workshop"))

    def add_dupe(db) -> None:  # type: ignore[no-untyped-def]
        db.add(Keyword(text="ai   workshop"))

    _expect_integrity_error(add_dupe)
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(Keyword)) == 1


# --- offerings ------------------------------------------------------------------------


def test_offering_name_key_is_unique_case_insensitively(db_url: str) -> None:
    def add_dupe(db) -> None:  # type: ignore[no-untyped-def]
        db.add(
            Offering(group_id=1, name="  products ", description="dupe", sort_order=9)
        )

    _expect_integrity_error(add_dupe)


def test_new_offering_in_new_group_defaults_active_and_stamps_updated_at_on_edit(
    db_url: str,
) -> None:
    with session_scope() as db:
        group = OfferingGroup(name="Services+", sort_order=4)
        off = Offering(
            group=group,
            name="AI audits",
            description="Audit of AI readiness.",
            sort_order=5,
        )
        db.add(off)
        db.flush()
        assert group.name_key == "services+"
        assert off.active is True and off.name_key == "ai audits"
        first_updated = off.updated_at
        off_id = off.id
    with session_scope() as db:
        off = db.get(Offering, off_id)
        assert off is not None
        off.name = "AI Audits & Reviews"
        db.flush()
        assert off.name_key == "ai audits & reviews"
        assert off.updated_at >= first_updated


def test_offering_referenced_by_an_enquiry_cannot_be_hard_deleted(db_url: str) -> None:
    with session_scope() as db:
        db.add(_enquiry(offering_id=2, ai_offering_id=2))

    db = get_sessionmaker()()
    try:
        db.delete(db.get(Offering, 2))
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_unused_offering_can_be_hard_deleted(db_url: str) -> None:
    with session_scope() as db:
        db.delete(db.get(Offering, 4))
    with session_scope() as db:
        assert db.get(Offering, 4) is None
        assert db.scalar(select(func.count()).select_from(Offering)) == 3


def test_offering_group_with_offerings_cannot_be_deleted(db_url: str) -> None:
    db = get_sessionmaker()()
    try:
        db.delete(db.get(OfferingGroup, 1))
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


# --- runs + enquiries -----------------------------------------------------------------


def test_full_discovery_graph_persists_with_defaults_and_relationships(
    db_url: str,
) -> None:
    with session_scope() as db:
        kw = Keyword(text="ai workshop")
        run = ScrapeRun(mode="fixture", keywords_planned=1)
        db.add_all([kw, run])
        db.flush()
        assert run.status == "queued" and run.trigger == "manual"
        assert run.posts_seen == run.posts_new == run.posts_dupe == 0
        assert run.enquiries_triaged == run.triage_failed == run.errors_count == 0
        assert run.started_at is None and run.abort_reason is None

        rk = ScrapeRunKeyword(run=run, keyword_id=kw.id, keyword_text=kw.text)
        enq = _enquiry(date_posted=date(2026, 10, 25), author_name="Priya")
        db.add_all([rk, enq])
        db.flush()
        assert rk.status == "pending" and rk.posts_seen == 0 and rk.errors == 0
        assert enq.review_status == "pending_review" and enq.triage_status == "pending"
        assert enq.stage is None and enq.ai_verdict is None
        assert enq.date_added.tzinfo is not None

        enq.sources.append(
            EnquirySource(
                source_ref="urn:li:activity:7001",
                source_url="https://x/1",
                first_seen_run_id=run.id,
            )
        )
        enq.keyword_hits.append(
            EnquiryKeywordHit(run_id=run.id, keyword_id=kw.id, keyword_text=kw.text)
        )
        enq.ai_extraction = {
            "verdict": "genuine",
            "priority": "high",
            "offering_id": 1,
            "email": None,
        }
        enq.ai_verdict = "genuine"
        db.add(
            LlmCall(
                feature="triage",
                model="claude-haiku-5-5",
                enquiry_id=enq.id,
                run_id=run.id,
                input_tokens=1200,
                output_tokens=180,
                cost_usd_micros=2100,
                latency_ms=900,
                fewshot_count=2,
                status="ok",
            )
        )
        db.add(
            TriageDecision(
                enquiry_id=enq.id,
                decision="accept",
                ai_verdict="genuine",
                is_override=False,
                field_overrides={"priority": ["low", "high"], "offering_id": [2, 1]},
            )
        )
        db.add(
            StatusHistory(
                entity_type="enquiry",
                entity_id=enq.id,
                field="review_status",
                from_value="pending_review",
                to_value="accepted",
            )
        )
        enq_id, run_id = enq.id, run.id

    with session_scope() as db:
        enq = db.get(Enquiry, enq_id)
        assert enq is not None
        assert [s.source_ref for s in enq.sources] == ["urn:li:activity:7001"]
        assert [h.keyword_text for h in enq.keyword_hits] == ["ai workshop"]
        assert enq.ai_extraction == {
            "verdict": "genuine",
            "priority": "high",
            "offering_id": 1,
            "email": None,
        }
        assert enq.date_posted == date(2026, 10, 25)
        run = db.get(ScrapeRun, run_id)
        assert run is not None and [k.keyword_text for k in run.keywords] == [
            "ai workshop"
        ]
        call = db.execute(
            select(LlmCall).where(LlmCall.enquiry_id == enq_id)
        ).scalar_one()
        assert (
            call.fewshot_count == 2
            and call.cost_usd_micros == 2100
            and call.created_at.tzinfo is not None
        )
        dec = db.execute(
            select(TriageDecision).where(TriageDecision.enquiry_id == enq_id)
        ).scalar_one()
        assert dec.field_overrides == {
            "priority": ["low", "high"],
            "offering_id": [2, 1],
        }
        assert dec.decided_at.tzinfo is not None
        hist = db.execute(select(StatusHistory)).scalar_one()
        assert hist.changed_at.tzinfo is not None and hist.to_value == "accepted"


def test_content_hash_and_source_ref_are_unique(db_url: str) -> None:
    with session_scope() as db:
        enq = _enquiry()
        enq.sources.append(EnquirySource(source_ref="urn:li:activity:1"))
        db.add(enq)

    _expect_integrity_error(lambda db: db.add(_enquiry(raw_text="different")))

    def dupe_ref(db) -> None:  # type: ignore[no-untyped-def]
        other = _enquiry(content_hash="b" * 64)
        other.sources.append(EnquirySource(source_ref="urn:li:activity:1"))
        db.add(other)

    _expect_integrity_error(dupe_ref)


def test_keyword_hit_is_unique_per_enquiry_run_and_keyword_text(db_url: str) -> None:
    with session_scope() as db:
        run = ScrapeRun(mode="fixture")
        enq = _enquiry()
        db.add_all([run, enq])
        db.flush()
        db.add(
            EnquiryKeywordHit(
                enquiry_id=enq.id, run_id=run.id, keyword_text="ai workshop"
            )
        )
        enq_id, run_id = enq.id, run.id

    _expect_integrity_error(
        lambda db: db.add(
            EnquiryKeywordHit(
                enquiry_id=enq_id, run_id=run_id, keyword_text="ai workshop"
            )
        )
    )
    with session_scope() as db:  # a different keyword in the same run is fine
        db.add(
            EnquiryKeywordHit(
                enquiry_id=enq_id, run_id=run_id, keyword_text="ai consulting"
            )
        )
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(EnquiryKeywordHit)) == 2


def test_deleting_a_keyword_keeps_run_rows_and_hits_with_their_text_snapshot(
    db_url: str,
) -> None:
    with session_scope() as db:
        kw = Keyword(text="ai workshop")
        run = ScrapeRun(mode="fixture")
        enq = _enquiry()
        db.add_all([kw, run, enq])
        db.flush()
        db.add(
            ScrapeRunKeyword(
                run_id=run.id, keyword_id=kw.id, keyword_text=kw.text, status="done"
            )
        )
        db.add(
            EnquiryKeywordHit(
                enquiry_id=enq.id, run_id=run.id, keyword_id=kw.id, keyword_text=kw.text
            )
        )
        kw_id = kw.id
    with session_scope() as db:
        db.delete(db.get(Keyword, kw_id))
    with session_scope() as db:
        rk = db.execute(select(ScrapeRunKeyword)).scalar_one()
        hit = db.execute(select(EnquiryKeywordHit)).scalar_one()
        assert rk.keyword_id is None and rk.keyword_text == "ai workshop"
        assert hit.keyword_id is None and hit.keyword_text == "ai workshop"


def test_deleting_an_enquiry_cascades_to_sources_and_hits_at_the_database_level(
    db_url: str,
) -> None:
    with session_scope() as db:
        enq = _enquiry()
        enq.sources.append(EnquirySource(source_ref="urn:li:activity:9"))
        enq.keyword_hits.append(EnquiryKeywordHit(keyword_text="ai workshop"))
        db.add(enq)
        db.flush()
        enq_id = enq.id
    with session_scope() as db:
        db.execute(Enquiry.__table__.delete().where(Enquiry.id == enq_id))
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(EnquirySource)) == 0
        assert db.scalar(select(func.count()).select_from(EnquiryKeywordHit)) == 0


# --- check constraints (enum-like values) ---------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: _enquiry(review_status="maybe"), id="enquiry.review_status"
        ),
        pytest.param(
            lambda: _enquiry(triage_status="later"), id="enquiry.triage_status"
        ),
        pytest.param(lambda: _enquiry(ai_verdict="spam"), id="enquiry.ai_verdict"),
        pytest.param(lambda: _enquiry(priority="urgent"), id="enquiry.priority"),
        pytest.param(lambda: _enquiry(mode="remote"), id="enquiry.mode"),
        pytest.param(lambda: _enquiry(stage="won"), id="enquiry.stage"),
        pytest.param(lambda: _enquiry(source="email"), id="enquiry.source"),
        pytest.param(lambda: ScrapeRun(mode="demo"), id="scrape_run.mode"),
        pytest.param(
            lambda: ScrapeRun(mode="live", status="paused"), id="scrape_run.status"
        ),
        pytest.param(
            lambda: ScrapeRun(mode="live", trigger="cron"), id="scrape_run.trigger"
        ),
        pytest.param(
            lambda: ScrapeRun(mode="live", abort_reason="banned"),
            id="scrape_run.abort_reason",
        ),
        pytest.param(
            lambda: LlmCall(
                feature="triage",
                model="m",
                input_tokens=1,
                output_tokens=1,
                cost_usd_micros=1,
                latency_ms=1,
                status="maybe",
            ),
            id="llm_call.status",
        ),
        pytest.param(
            lambda: LlmCall(
                feature="chat",
                model="m",
                input_tokens=1,
                output_tokens=1,
                cost_usd_micros=1,
                latency_ms=1,
                status="ok",
            ),
            id="llm_call.feature",
        ),
        pytest.param(
            lambda: StatusHistory(entity_type="lead", entity_id=1, field="stage"),
            id="status_history.entity_type",
        ),
    ],
)
def test_invalid_enum_values_are_rejected_by_check_constraints(
    db_url: str, factory
) -> None:  # type: ignore[no-untyped-def]
    _expect_integrity_error(lambda db: db.add(factory()))


def test_invalid_decision_and_run_keyword_status_are_rejected(db_url: str) -> None:
    with session_scope() as db:
        enq = _enquiry()
        run = ScrapeRun(mode="live")
        db.add_all([enq, run])
        db.flush()
        enq_id, run_id = enq.id, run.id
    _expect_integrity_error(
        lambda db: db.add(
            TriageDecision(enquiry_id=enq_id, decision="maybe", is_override=False)
        )
    )
    _expect_integrity_error(
        lambda db: db.add(
            ScrapeRunKeyword(run_id=run_id, keyword_text="x", status="lost")
        )
    )


def test_linkedin_session_is_a_singleton_and_rejects_unknown_status(
    db_url: str,
) -> None:
    _expect_integrity_error(
        lambda db: db.add(LinkedInSession(id=2, status="not_connected"))
    )
    db = get_sessionmaker()()
    try:
        row = db.get(LinkedInSession, 1)
        assert row is not None
        row.status = "busy"  # derived from the job runner, never stored
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_missing_required_fields_are_rejected(db_url: str) -> None:
    _expect_integrity_error(
        lambda db: db.add(Enquiry(source="linkedin_post", raw_text="no hash"))
    )
    _expect_integrity_error(
        lambda db: db.add(Offering(name="No group", description="", sort_order=1))
    )


# --- users ----------------------------------------------------------------------------


def test_users_email_is_unique_but_nullable(db_url: str) -> None:
    with session_scope() as db:
        db.add_all(
            [
                User(display_name="A"),
                User(display_name="B"),
                User(display_name="C", email="c@x.example"),
            ]
        )
    _expect_integrity_error(
        lambda db: db.add(User(display_name="D", email="c@x.example"))
    )
    with session_scope() as db:
        u = db.execute(select(User).where(User.email == "c@x.example")).scalar_one()
        db.add(Keyword(text="owned keyword", actor_user_id=u.id))


# --- datetimes ------------------------------------------------------------------------


def test_datetimes_round_trip_as_aware_utc_and_filter_correctly_with_aware_bounds(
    db_url: str,
) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    base = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
    with session_scope() as db:
        for i, ts in enumerate(
            [base - timedelta(days=1), base, base + timedelta(hours=1)]
        ):
            db.add(
                LlmCall(
                    feature="triage",
                    model="m",
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd_micros=10 + i,
                    latency_ms=1,
                    status="ok",
                    created_at=ts,
                )
            )
        # A non-UTC aware value is normalised to UTC on write.
        db.add(
            LlmCall(
                feature="ping",
                model="m",
                input_tokens=1,
                output_tokens=1,
                cost_usd_micros=100,
                latency_ms=1,
                status="ok",
                created_at=datetime(2026, 10, 27, 12, 0, tzinfo=ist),
            )
        )

    with session_scope() as db:
        start = datetime(2026, 10, 27, 0, 0, tzinfo=ist)  # == 2026-10-26 18:30 UTC
        end = start + timedelta(days=1)
        total = db.scalar(
            select(func.sum(LlmCall.cost_usd_micros)).where(
                LlmCall.created_at >= start, LlmCall.created_at < end
            )
        )
        assert total == 11 + 12 + 100
        ping = db.execute(select(LlmCall).where(LlmCall.feature == "ping")).scalar_one()
        assert ping.created_at == datetime(2026, 10, 27, 6, 30, tzinfo=UTC)
        assert ping.created_at.utcoffset() == timedelta(0)

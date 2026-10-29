"""Weekly counters computed from the CRM (no sampling, no LLM). One bounded query per source;
rows are bucketed into local ISO weeks in Python (timezone-correct and dialect-neutral).

Mapping decisions (also in spec/capabilities/metrics-dashboard.md):

* scanned            = seen_posts.first_seen_at in the week (every post the scraper looked at).
* enquiries added    = relevant enquiries created that week with origin discovery or manual
                       (AI verdict genuine; manual ones unless discarded). Sheet/import origins are
                       the baseline, never counted here.
* contacted          = prospects whose first real (non-estimated) contact date falls in the week.
* meetings/proposals = distinct leads that got a meeting or proposal event that week.
* old prospects      = distinct prospects created BEFORE the week that were researched or whose
                       tracked profile changed that week (timeline kinds research / tracked_changed).
* messages sent      = sent outreach emails + manual contact records whose channel is not email.
* LinkedIn outreach  = contact records on a prospect whose channel starts with ``linkedin``.
* first follow-up    = distinct prospects moved to Follow-up 1 that week.
* profile watch      = tracked profiles actually visited by the watch job that week (``watch:<slug>`` log rows of
                       runs started that week with status done/warning, from a watch run or one appended to
                       discovery). Manual track/untrack timeline events are not watch activity.
"""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from sales0.db.models import (
    Enquiry,
    OutreachMessage,
    Prospect,
    ScrapeRun,
    ScrapeRunKeyword,
    SeenPost,
    TimelineEvent,
)
from sales0.services.metrics.weeks import Bucketer, Week

ORIGINS_COUNTED = ("discovery", "manual")


def relevant_clause():  # noqa: ANN201
    """SQL predicate: a relevant (counted) enquiry."""
    return and_(
        Enquiry.origin.in_(ORIGINS_COUNTED),
        or_(Enquiry.ai_verdict == "genuine", and_(Enquiry.origin == "manual", Enquiry.review_status != "discarded")),
    )


def _is_contact(data: dict) -> bool:
    to = data.get("to")
    return to == "contacted" or "contacted_at" in data or (data.get("from") == "new" and to not in (None, "new"))


def compute_counters(db: Session, weeks: list[Week]) -> dict[str, list[int]]:
    b = Bucketer(weeks)
    lo, hi = b.lo, b.hi
    out: dict[str, list[int]] = {}

    out["scanned"] = b.count(
        db.scalars(select(SeenPost.first_seen_at).where(SeenPost.first_seen_at >= lo, SeenPost.first_seen_at < hi))
    )
    enq = db.execute(
        select(Enquiry.date_added, Enquiry.origin).where(relevant_clause(), Enquiry.date_added >= lo, Enquiry.date_added < hi)
    ).all()
    out["enquiries_added"] = b.count(t for t, _ in enq)
    out["src_linkedin_keyword"] = b.count(t for t, o in enq if o == "discovery")
    out["src_manual"] = b.count(t for t, o in enq if o == "manual")

    out["enquiries_contacted"] = b.count(
        db.scalars(
            select(Prospect.contacted_at).where(
                Prospect.contacted_at >= lo, Prospect.contacted_at < hi, Prospect.contacted_estimated.is_(False)
            )
        )
    )

    stage_rows = db.execute(
        select(TimelineEvent.prospect_id, TimelineEvent.data, TimelineEvent.occurred_at).where(
            TimelineEvent.kind == "stage_change",
            TimelineEvent.prospect_id.is_not(None),
            TimelineEvent.occurred_at >= lo,
            TimelineEvent.occurred_at < hi,
        )
    ).all()
    linkedin: list[tuple[object, object]] = []
    manual_contacts: list = []
    follow1: list[tuple[object, object]] = []
    for pid, data, at in stage_rows:
        d = data or {}
        if _is_contact(d):
            channel = str(d.get("channel") or "manual").lower()
            if channel.startswith("linkedin"):
                linkedin.append((pid, at))
            if channel != "email":
                manual_contacts.append(at)
        if d.get("to") == "follow_up_1":
            follow1.append((pid, at))
    out["linkedin_outreach"] = b.distinct(linkedin)  # type: ignore[arg-type]
    out["first_follow_up"] = b.distinct(follow1)  # type: ignore[arg-type]
    emails = b.count(
        db.scalars(select(OutreachMessage.sent_at).where(OutreachMessage.state == "sent", OutreachMessage.sent_at >= lo, OutreachMessage.sent_at < hi))
    )
    manual = b.count(manual_contacts)
    out["messages_sent"] = [e + m for e, m in zip(emails, manual, strict=True)]
    out["emails_sent"] = emails

    lead_rows = db.execute(
        select(TimelineEvent.subject_id, TimelineEvent.kind, TimelineEvent.data, TimelineEvent.occurred_at).where(
            TimelineEvent.subject_type == "lead",
            TimelineEvent.kind.in_(("lead_created", "lead_stage", "proposal_sent")),
            TimelineEvent.occurred_at >= lo,
            TimelineEvent.occurred_at < hi,
        )
    ).all()
    wanted = ("meeting_scheduled", "proposal_sent")
    out["meetings_proposals"] = b.distinct(
        (lid, at) for lid, kind, data, at in lead_rows
        if kind == "proposal_sent" or ((data or {}).get("stage") if kind == "lead_created" else (data or {}).get("to")) in wanted
    )

    old_rows = db.execute(
        select(TimelineEvent.prospect_id, TimelineEvent.kind, TimelineEvent.occurred_at, Prospect.created_at)
        .join(Prospect, Prospect.id == TimelineEvent.prospect_id)
        .where(
            TimelineEvent.kind.in_(("research", "tracked_changed")),
            TimelineEvent.occurred_at >= lo,
            TimelineEvent.occurred_at < hi,
        )
    ).all()
    pairs = []
    for pid, kind, at, created in old_rows:
        i = b.index(at)
        if i is not None and created is not None and created < weeks[i].start_utc:
            pairs.append((pid, at))
    out["old_prospects"] = b.distinct(pairs)
    out["profile_watch"] = b.count(
        db.scalars(
            select(ScrapeRun.started_at)
            .join(ScrapeRunKeyword, ScrapeRunKeyword.run_id == ScrapeRun.id)
            .where(
                ScrapeRunKeyword.keyword_text.like("watch:%"),
                ScrapeRunKeyword.status.in_(("done", "warning")),
                ScrapeRun.started_at >= lo,
                ScrapeRun.started_at < hi,
            )
        )
    )
    return out


def app_first_activity(db: Session):  # noqa: ANN201
    """Earliest instant the app itself recorded work (scan, run, own enquiry). Sheet syncs don't count."""
    candidates = [
        db.scalar(select(SeenPost.first_seen_at).order_by(SeenPost.first_seen_at).limit(1)),
        db.scalar(select(ScrapeRun.started_at).where(ScrapeRun.started_at.is_not(None)).order_by(ScrapeRun.started_at).limit(1)),
        db.scalar(select(Enquiry.date_added).where(Enquiry.origin.in_(ORIGINS_COUNTED)).order_by(Enquiry.date_added).limit(1)),
        db.scalar(select(OutreachMessage.created_at).order_by(OutreachMessage.created_at).limit(1)),
    ]
    present = [c for c in candidates if c is not None]
    return min(present) if present else None


__all__ = ["ORIGINS_COUNTED", "app_first_activity", "compute_counters", "relevant_clause"]

"""Deterministic metrics fixture: 7 ISO weeks (2026-W38..W44, Asia/Kolkata), 328+ genuine enquiries and
2090 seen posts, with every expected weekly count written down in ``EXPECTED`` independently of the code
under test. Synthetic data only. "now" is Wednesday 2026-10-28 12:00 IST (inside W44)."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from sales0.db.models import (
    Enquiry,
    EnquiryKeywordHit,
    Keyword,
    Lead,
    LlmCall,
    OutreachMessage,
    Prospect,
    ScrapeRun,
    ScrapeRunKeyword,
    SeenPost,
    SheetMetric,
    SheetMetricWeek,
    TeamMember,
    TimelineEvent,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=IST).astimezone(UTC)
WEEKS = [f"2026-W{n}" for n in range(38, 45)]
MONDAYS = [date.fromisocalendar(2026, n, 1) for n in range(38, 45)]

ENQ = [40, 45, 50, 55, 48, 52, 38]  # discovery genuine, counted
MAN = [0, 0, 2, 2, 2, 3, 3]  # manual, counted
NOISE_DISCARD = 4  # discovery with verdict "discard", never counted
NOISE_SHEET = 2  # origin "sheet", never counted
SEEN = [290, 300, 310, 300, 320, 310, 260]
CONT = [10, 12, 15, 14, 16, 15, 9]  # prospects first contacted that week (real date)
CONT_ESTIMATED = 2  # estimated contact dates, never counted
LINKEDIN = [2, 3, 3, 4, 5, 4, 2]
MANUAL_CH = 2
FU_EMAILS = 3  # extra sent emails that are not first contacts
REPLY = [3, 4, 5, 4, 6, 5, 2]  # leads opened (meeting) that week
PROPOSAL = [1, 2, 2, 2, 3, 2, 1]
WON = [0, 1, 1, 0, 1, 1, 0]
FIRST_FU = [4, 5, 6, 5, 7, 6, 3]
OLD = [0, 2, 3, 2, 4, 3, 1]
WATCH = [0, 0, 2, 1, 3, 2, 1]
ALPHA_SHARE = 3  # of every 5 discovery enquiries come from kw_alpha
ACCEPTED_UNCONTACTED = 2

EXPECTED = {
    "scanned": SEEN,
    "enquiries_added": [a + b for a, b in zip(ENQ, MAN, strict=True)],
    "enquiries_contacted": CONT,
    "meetings_proposals": REPLY,
    "old_prospects": OLD,
    "src_linkedin_keyword": ENQ,
    "src_manual": MAN,
    "linkedin_outreach": LINKEDIN,
    "messages_sent": [c + FU_EMAILS for c in CONT],
    "first_follow_up": FIRST_FU,
    "profile_watch": WATCH,
}


def _stamps(week: int, n: int) -> list[datetime]:
    """n instants inside the week: the first exactly at Monday 00:00 IST, the last 1 s before the next Monday."""
    start = datetime(MONDAYS[week].year, MONDAYS[week].month, MONDAYS[week].day, tzinfo=IST).astimezone(UTC)
    span = 7 * 24 * 3600 - 1
    return [start + timedelta(seconds=round(span * k / (n - 1))) for k in range(n)]


def _h(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def seed(db: Session) -> dict[str, int]:
    member = TeamMember(name="Seed Member", email="seed@example.test")
    db.add(member)
    kw = {t: Keyword(text=t, text_key=t) for t in ("kw_alpha", "kw_beta", "kw_dead")}
    db.add_all(kw.values())
    db.flush()

    # ---- seen posts + their triage cost (1000 micros each, at the post's time)
    for w in range(7):
        run = ScrapeRun(trigger="manual", mode="live", status="succeeded", started_at=_stamps(w, 3)[1], finished_at=_stamps(w, 3)[1])
        db.add(run)
        db.flush()
        alpha_seen, dead_seen = 150, 20
        for text, n in (("kw_alpha", alpha_seen), ("kw_beta", SEEN[w] - alpha_seen - dead_seen), ("kw_dead", dead_seen)):
            db.add(ScrapeRunKeyword(run_id=run.id, keyword_id=kw[text].id, keyword_text=text, status="done", posts_seen=n))
        for k, at in enumerate(_stamps(w, SEEN[w])):
            text = "kw_alpha" if k < alpha_seen else "kw_dead" if k >= SEEN[w] - dead_seen else "kw_beta"
            sp = SeenPost(
                fingerprint=_h("sp", w, k), content_hash=_h("spc", w, k), text_prefix="synthetic post", keyword_text=text,
                triage_status="done", verdict="discard", first_seen_at=at, last_seen_at=at,
            )
            db.add(sp)
            db.flush()
            db.add(LlmCall(feature="triage", model="claude-haiku-5-5", seen_post_id=sp.id, input_tokens=1, output_tokens=1,
                           cost_usd_micros=1000, latency_ms=1, status="ok", created_at=at))
        db.flush()

    # ---- enquiries
    legacy = []
    for i in range(5):
        p = Prospect(name=f"Legacy {i}", created_at=datetime(2026, 8, 1, tzinfo=UTC), updated_at=datetime(2026, 8, 1, tzinfo=UTC))
        db.add(p)
        legacy.append(p)
    db.flush()
    n_alpha_total = 0
    alpha_prospects = 0
    for w in range(7):
        stamps = _stamps(w, ENQ[w])
        enq_ids: list[int] = []
        for k, at in enumerate(stamps):
            e = Enquiry(content_hash=_h("e", w, k), raw_text="synthetic enquiry", date_added=at, origin="discovery",
                        ai_verdict="genuine", triage_status="done", review_status="accepted" if k < CONT[w] + ACCEPTED_UNCONTACTED else "pending_review")
            db.add(e)
            db.flush()
            enq_ids.append(e.id)
            text = "kw_alpha" if k * 5 < ENQ[w] * ALPHA_SHARE else "kw_beta"
            n_alpha_total += text == "kw_alpha"
            alpha_prospects += text == "kw_alpha" and k < CONT[w] + ACCEPTED_UNCONTACTED
            db.add(EnquiryKeywordHit(enquiry_id=e.id, keyword_id=kw[text].id, keyword_text=text, seen_at=at))
        for k in range(MAN[w]):
            db.add(Enquiry(content_hash=_h("m", w, k), raw_text="manual", date_added=stamps[k + 1], origin="manual", triage_status="pending"))
        for k in range(NOISE_DISCARD):
            db.add(Enquiry(content_hash=_h("d", w, k), raw_text="discard", date_added=stamps[k + 2], origin="discovery", ai_verdict="discard", triage_status="done"))
        for k in range(NOISE_SHEET):
            db.add(Enquiry(content_hash=_h("s", w, k), raw_text="sheet", date_added=stamps[k + 3], origin="sheet"))
        db.flush()

        # ---- prospects first contacted this week
        week_start = stamps[0]
        later = week_start + timedelta(hours=30)
        for k in range(CONT[w] + ACCEPTED_UNCONTACTED):
            contacted = k < CONT[w]
            p = Prospect(
                name=f"P{w}-{k}", origin_enquiry_id=enq_ids[k], stage="contacted" if contacted else "new",
                contacted_at=later if contacted else None, contacted_estimated=False,
                responded_at=later if contacted and k < REPLY[w] else None, created_at=week_start, updated_at=week_start,
            )
            db.add(p)
            db.flush()
            db.get(Enquiry, enq_ids[k]).prospect_id = p.id  # type: ignore[union-attr]
            if not contacted:
                continue
            channel = "linkedin" if k < LINKEDIN[w] else "manual" if k < LINKEDIN[w] + MANUAL_CH else "email"
            db.add(TimelineEvent(subject_type="prospect", subject_id=p.id, prospect_id=p.id, kind="stage_change", occurred_at=later,
                                 data={"from": "new", "to": "contacted", "channel": channel}))
            if channel == "email":
                db.add(OutreachMessage(member_id=member.id, connection_email="seed@example.test", from_addr="seed@example.test",
                                       state="sent", sent_at=later, prospect_id=p.id, created_at=later))
            if k < FIRST_FU[w]:
                for _ in range(2):  # a duplicate event must not double count
                    db.add(TimelineEvent(subject_type="prospect", subject_id=p.id, prospect_id=p.id, kind="stage_change",
                                         occurred_at=later + timedelta(days=1), data={"from": "contacted", "to": "follow_up_1"}))
            if k < REPLY[w]:
                ld = Lead(prospect_id=p.id, stage="won" if k < WON[w] else "proposal_sent" if k < PROPOSAL[w] else "meeting_scheduled",
                          proposal_sent_on=date(2026, 1, 1) if k < PROPOSAL[w] else None, opened_at=later)
                db.add(ld)
                db.flush()
                db.add(TimelineEvent(subject_type="lead", subject_id=ld.id, prospect_id=p.id, kind="lead_created", occurred_at=later,
                                     data={"stage": "meeting_scheduled", "opened_by": "reply"}))
                if k < PROPOSAL[w]:
                    db.add(TimelineEvent(subject_type="lead", subject_id=ld.id, prospect_id=p.id, kind="proposal_sent", occurred_at=later + timedelta(hours=1),
                                         data={"from": "meeting_scheduled", "to": "proposal_sent"}))
                if k < WON[w]:
                    db.add(TimelineEvent(subject_type="lead", subject_id=ld.id, prospect_id=p.id, kind="lead_stage", occurred_at=later + timedelta(hours=2),
                                         data={"from": "proposal_sent", "to": "won"}))
        # estimated contact dates (sheet-style) are never counted
        for k in range(CONT_ESTIMATED):
            db.add(Prospect(name=f"Est {w}-{k}", stage="contacted", contacted_at=later, contacted_estimated=True, created_at=week_start, updated_at=week_start))
        # extra sent emails that are not first contacts + non-sent noise
        for k in range(FU_EMAILS):
            db.add(OutreachMessage(member_id=member.id, connection_email="seed@example.test", from_addr="seed@example.test", state="sent", sent_at=later, created_at=later))
        db.add(OutreachMessage(member_id=member.id, connection_email="seed@example.test", from_addr="seed@example.test", state="draft", created_at=later))
        # old-prospect lookups and tracked-profile changes (legacy prospects, created before every week)
        for k in range(OLD[w]):
            for kind in ("research", "research"):
                db.add(TimelineEvent(subject_type="prospect", subject_id=legacy[k].id, prospect_id=legacy[k].id, kind=kind, occurred_at=later))
        for k in range(WATCH[w]):
            db.add(TimelineEvent(subject_type="prospect", subject_id=legacy[k].id, prospect_id=legacy[k].id, kind="tracked_changed", occurred_at=later))
        # a research event on a prospect created this very week is not an "old" prospect
        fresh = db.query(Prospect).filter(Prospect.name == f"P{w}-0").one()
        db.add(TimelineEvent(subject_type="prospect", subject_id=fresh.id, prospect_id=fresh.id, kind="research", occurred_at=later))
    db.flush()
    return {"alpha_prospects": alpha_prospects, "alpha_relevant": n_alpha_total, "beta_relevant": sum(ENQ) - n_alpha_total}


def seed_sheet_baseline(db: Session) -> None:
    """Weekly sheet rows: W36/W37 before the app existed, W38 lower than computed, W39 higher."""
    weekly = SheetMetric(metric_set="weekly", metric="Number of Enquiries added", metric_key="weekly::enquiries_added")
    db.add(weekly)
    sec = SheetMetric(metric_set="secondary", group_label="Outbound", metric="Messages Sent", metric_key="secondary::messages_sent", weekly_target=30.0)
    li = SheetMetric(metric_set="secondary", group_label="Enquiries added", metric="LinkedIn Scraping", metric_key="secondary::li", weekly_target=15.0)
    wa = SheetMetric(metric_set="secondary", group_label="Enquiries added", metric="WhatsApp Group Scraping", metric_key="secondary::wa", weekly_target=5.0)
    db.add_all([sec, li, wa])
    db.flush()
    for week, value in ((36, 7.0), (37, 9.0), (38, 5.0), (39, 999.0)):
        db.add(SheetMetricWeek(metric_id=weekly.id, week_no=week, occurrence=1, label=f"Week {week}", raw=str(value), value=value))
    db.add(SheetMetricWeek(metric_id=sec.id, week_no=37, occurrence=1, label="Week 37", raw="11", value=11.0))

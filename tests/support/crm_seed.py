"""Seed builders for the Phase 2 journey and the realistic-migration tests (S6).

Two families, both synthetic (no personal data):

* ``seed_pending_enquiries`` - ORM rows at HEAD: pending-review, already-triaged enquiries that
  share one LinkedIn author, so the Inbox shows cards the journey can accept.
* ``seed_realistic_db`` - raw SQL against an OLDER schema revision (p4/p5): a populated database
  (>= 200 enquiries, accepted ones sharing an author, outreach messages with events, replies)
  used to prove a migration keeps every child row. Raw SQL, because the ORM models describe HEAD.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from sales0.db.models import Enquiry, LinkedinProfile

STAMP = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
AUTHOR_SLUG = "in/priya-menon-test"
AUTHOR_URL = f"https://www.linkedin.com/{AUTHOR_SLUG}/"


@dataclass(frozen=True)
class SeededEnquiries:
    profile_id: int
    enquiry_ids: tuple[int, ...]


def seed_pending_enquiries(
    db: Session,
    *,
    count: int = 2,
    slug: str = AUTHOR_SLUG,
    company: str = "Acme Logistics",
    email: str = "priya@acme.example",
    author: str = "Priya Menon",
) -> SeededEnquiries:
    """``count`` triaged, pending-review enquiries by ONE LinkedIn author (genuine, high priority)."""
    profile = LinkedinProfile(
        slug=slug, kind="person", profile_url=f"https://www.linkedin.com/{slug}/", display_name=author,
        first_seen_at=STAMP, last_seen_at=STAMP,
    )
    db.add(profile)
    db.flush()
    ids: list[int] = []
    for n in range(1, count + 1):
        row = Enquiry(
            content_hash=f"{slug}-{n}".encode().hex().ljust(64, "0")[:64],
            raw_text=f"{company} is looking for a two-day GenAI workshop for 40 warehouse planners in Pune (request {n}).",
            review_status="pending_review", triage_status="done", ai_verdict="genuine", ai_reason="A company asking for training.",
            ai_poster_type="buyer_company", ai_priority="high", priority="high",
            requirement_summary=f"{company} - GenAI workshop {n} - Pune | Offline",
            author_name=author, author_profile_id=profile.id, author_profile_url=profile.profile_url,
            company=company, contact_person=author, email=email,
            date_added=STAMP + timedelta(minutes=n),
        )
        db.add(row)
        db.flush()
        ids.append(row.id)
    return SeededEnquiries(profile.id, tuple(ids))


# --------------------------------------------------------------------------- old-schema seeding
NOW_S = "2026-10-01 09:00:00.000000"
SENT_S = "2026-10-02 10:00:00.000000"


@dataclass(frozen=True)
class RealisticSeed:
    enquiries: int
    messages: int
    events: int
    replies: int


def _insert_enquiry(c, n: int, profile_id: int | None, accepted: bool) -> int:  # type: ignore[no-untyped-def]
    c.execute(
        text(
            "INSERT INTO enquiries (source, content_hash, raw_text, date_added, review_status, stage, triage_status, decided_at, company,"
            " contact_person, email, author_profile_id, author_name, requirement_summary)"
            " VALUES ('linkedin_post', :h, :t, :da, :rs, :stage, 'done', :dec, :co, :cp, :em, :apid, 'Author', :t)"
        ),
        {
            "h": f"{n:064d}", "t": f"We need AI training for team {n}", "da": NOW_S,
            "rs": "accepted" if accepted else "pending_review", "stage": "prospect_new" if accepted else None,
            "dec": NOW_S if accepted else None, "co": f"Company {n % 40}", "cp": f"Person {n}", "em": f"p{n % 60}@co{n % 40}.example",
            "apid": profile_id,
        },
    )
    return int(c.execute(text("SELECT MAX(id) FROM enquiries")).scalar_one())


def seed_realistic_db(engine: Engine, *, with_replies: bool, enquiries: int = 210, messages: int = 24, events_per_message: int = 4) -> RealisticSeed:
    """Populate a DB that is at the p4 or p5 schema (pass ``with_replies=True`` only at p5+, where ``outreach_replies`` exists).

    * ``enquiries`` rows (>= 200): the first 60 accepted and spread over 5 authors (so several accepted enquiries share an author),
      the rest pending review; 25 have no author profile.
    * ``messages`` outreach messages (half sent) each with ``events_per_message`` outreach_events (the CASCADE children);
      with replies, one human reply for every third message.
    """
    with engine.begin() as c:
        c.execute(text("INSERT INTO team_members (name, email, role, active, created_at) VALUES ('Ann', 'ann@t.example', 'member', 1, :n)"), {"n": NOW_S})
        for i in range(5):
            slug = f"in/author-{i}"
            c.execute(
                text("INSERT INTO linkedin_profiles (slug, kind, profile_url, display_name, first_seen_at, last_seen_at, posts_seen, relevant_count)"
                     " VALUES (:s, 'person', :u, :d, :n, :n, 3, 1)"),
                {"s": slug, "u": f"https://www.linkedin.com/{slug}/", "d": f"Author {i}", "n": NOW_S},
            )
        profile_ids = [r[0] for r in c.execute(text("SELECT id FROM linkedin_profiles ORDER BY id"))]
        enquiry_ids = []
        for n in range(1, enquiries + 1):
            pid = None if n > enquiries - 25 else profile_ids[n % len(profile_ids)]
            enquiry_ids.append(_insert_enquiry(c, n, pid, accepted=n <= 60))
        message_ids = []
        for m in range(messages):
            sent = m % 2 == 0
            c.execute(
                text("INSERT INTO outreach_messages (member_id, connection_email, from_addr, to_addrs, cc_addrs, subject, body, state, enquiry_id,"
                     " sent_at, send_outcome_unknown, created_at, updated_at)"
                     " VALUES (1, 'ann@t.example', 'ann@t.example', :to, '', :subj, 'Body', :st, :enq, :sent, 0, :n, :n)"),
                {"to": f"p{m}@co{m}.example", "subj": f"Hello {m}", "st": "sent" if sent else "draft", "enq": enquiry_ids[m],
                 "sent": SENT_S if sent else None, "n": NOW_S},
            )
            message_ids.append(int(c.execute(text("SELECT MAX(id) FROM outreach_messages")).scalar_one()))
        events = 0
        for mid in message_ids:
            for k in range(events_per_message):
                c.execute(text("INSERT INTO outreach_events (message_id, kind, created_at) VALUES (:m, :k, :n)"),
                          {"m": mid, "k": ("drafted", "edited", "approved", "sent")[k % 4], "n": NOW_S})
                events += 1
        replies = 0
        if with_replies:
            for i, mid in enumerate(message_ids[::3]):
                c.execute(
                    text("INSERT INTO outreach_replies (message_id, member_id, gmail_message_id, gmail_thread_id, from_addr, received_at, snippet,"
                         " kind, intent, classified_by, handled, created_at)"
                         " VALUES (:m, 1, :g, 't', 'p@acme.example', :n, 's', 'human', 'interested', 'llm', 0, :n)"),
                    {"m": mid, "g": f"g{i}", "n": NOW_S},
                )
                replies += 1
    return RealisticSeed(enquiries, messages, events, replies)

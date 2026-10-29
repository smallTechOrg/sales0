"""Our own data about a prospect, as plain text for the AI (no new scraping).

Everything here comes from the CRM: the prospect, its company and characteristics, linked enquiries (the scraped
post text and the author's headline), contacts and their titles, sheet rows, our notes, outreach messages and
replies, follow-ups and the timeline, the latest research and the LinkedIn company page text. It is passed to the
model as trusted-but-quoted context: the prompts wrap it in delimiters, and third-party text quoted in it (posts,
replies, sheet cells, page text) stays DATA. Sizes are capped so a long history cannot blow up the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Company, Enquiry, OutreachMessage, Prospect
from sales0.services.crm import notes as notes_service
from sales0.services.crm import prospects as prospects_service
from sales0.services.research import docs


def _one_line(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _block(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Basics:
    name: str
    company_name: str | None
    website: str | None
    company_linkedin_url: str | None
    company_id: int | None


def basics(db: Session, prospect_id: int) -> Basics:
    p = docs.require_prospect(db, prospect_id)
    company = db.get(Company, p.company_id) if p.company_id else None
    return Basics(
        name=p.name, company_name=company.name if company else None, website=company.website if company else None,
        company_linkedin_url=company.linkedin_url if company else None, company_id=company.id if company else None,
    )  # fmt: skip


def _brief_lines(data: dict[str, Any]) -> list[str]:
    out = []
    for key, label in (("what_they_do", "What they do"), ("size_hints", "Size"), ("suggested_offering", "Suggested fit")):
        value = str(data.get(key) or "").strip()
        if value and value.lower() != "unknown":
            out.append(f"{label}: {_one_line(value, 400)}")
    for key, label in (("ai_training_needs", "Possible AI-training needs"), ("personalisation_hooks", "Hooks")):
        items = [str(x) for x in data.get(key) or [] if str(x).strip()]
        if items:
            out.append(f"{label}: " + "; ".join(_one_line(x, 200) for x in items[:4]))
    return out


def build(db: Session, prospect_id: int, *, detail: str = "research", linkedin_text: str | None = None) -> str:
    """The records block. ``detail="research"`` (compact: who they are, what they asked, our notes) or
    ``"summary"`` (everything, including outreach, replies, follow-ups, timeline and the latest research)."""
    full = detail == "summary"
    p = prospects_service.get_prospect(db, prospect_id)
    row = db.get(Prospect, prospect_id)
    b = basics(db, prospect_id)
    lines: list[str] = [f"Prospect: {p.name} (stage {p.stage_label}, status {p.status}"
                        + (f", closed: {p.closed_reason}" if p.closed_reason else "") + ")"]
    if b.company_name:
        lines.append(f"Company: {b.company_name}" + (f" | website {b.website}" if b.website else "")
                     + (f" | LinkedIn {b.company_linkedin_url}" if b.company_linkedin_url else ""))
    if p.characteristics:
        lines.append("Characteristics: " + ", ".join(c.name for c in p.characteristics))
    if p.flags:
        lines.append("Flags: " + ", ".join(f.kind.replace("_", " ") for f in p.flags))
    if full:
        lines.append(
            "Contact: " + (f"contacted {p.contacted_at:%d %b %Y}" if p.contacted_at else "not contacted yet")
            + (f"; replied {p.responded_at:%d %b %Y}" if p.responded_at else "")
            + (f"; next follow-up {p.next_follow_up_on:%d %b %Y}" if p.next_follow_up_on else "")
        )
        if p.lead:
            lines.append(f"Lead: {p.lead.stage_label}" + (f", meeting {p.lead.meeting_at:%d %b %Y}" if p.lead.meeting_at else "")
                         + (f", proposal sent {p.lead.proposal_sent_on:%d %b %Y}" if p.lead.proposal_sent_on else ""))
        if p.client:
            lines.append(f"Client: {p.client.status}" + (f" since {p.client.started_on:%d %b %Y}" if p.client.started_on else ""))
    if p.reason_text:
        lines.append("Why this prospect: " + _one_line(p.reason_text, 300))

    # enquiries (scraped posts) with the author's headline
    enq_rows = list(db.scalars(select(Enquiry).where(Enquiry.prospect_id == prospect_id).order_by(Enquiry.date_added.desc()).limit(5)))
    if enq_rows:
        lines.append("")
        lines.append("Their enquiries / posts (their own words, quoted):")
        for e in enq_rows:
            head = f"- {e.date_added:%d %b %Y}" if e.date_added else "-"
            who = _one_line(e.author_name, 80) + (f" ({_one_line(e.author_headline, 160)})" if e.author_headline else "")
            lines.append(f"{head} {who}: {_one_line(e.requirement_summary, 200)}" if e.requirement_summary else f"{head} {who}")
            lines.append("  \"" + _one_line(e.raw_text, 900 if full else 700) + "\"")
            if e.sheet_status or e.sheet_notes:
                lines.append(f"  Sheet: {_one_line(e.sheet_status, 100)} {_one_line(e.sheet_notes, 300)}".rstrip())

    if p.contacts:
        lines.append("")
        lines.append("Contacts:")
        for c in p.contacts[:8]:
            lines.append("- " + ", ".join(x for x in (c.full_name, c.title, c.email if full else None) if x))

    if row is not None and (row.sheet_status or row.sheet_notes or row.sheet_fields):
        lines.append("")
        lines.append("Google Sheet row (read-only; their words where quoted):")
        if row.sheet_status:
            lines.append(f"- Status: {_one_line(row.sheet_status, 150)}")
        if row.sheet_notes:
            lines.append(f"- Notes: {_one_line(row.sheet_notes, 500)}")
        if isinstance(row.sheet_fields, dict):
            shown = 0
            for key, value in row.sheet_fields.items():
                if value in (None, "", []) or key in ("status", "notes"):
                    continue
                lines.append(f"- {_one_line(key, 40)}: {_one_line(value, 200)}")
                shown += 1
                if shown >= (12 if full else 6):
                    break

    our_notes = notes_service.latest_texts(db, prospect_id, limit=12 if full else 8)
    if our_notes:
        lines.append("")
        lines.append("Our own notes (newest first):")
        for created, author, body in our_notes:
            lines.append(f"- {created:%d %b %Y} {author}: {_one_line(body, 500)}")

    if full:
        research = docs.latest_usable(db, prospect_id, "research")
        if research is not None:
            lines.append("")
            lines.append(f"Latest research (version {research.version}, {research.origin_label}):")
            lines.extend("- " + x for x in _brief_lines(research.data))
        msgs = list(db.scalars(
            select(OutreachMessage).where(OutreachMessage.prospect_id == prospect_id)
            .order_by(OutreachMessage.created_at.desc()).limit(6)
        ))  # fmt: skip
        if p.messages or msgs:
            lines.append("")
            lines.append("Our emails (newest first):")
            bodies = {m.id: m.body for m in msgs}
            for m in p.messages[:6]:
                when = f"sent {m.sent_at:%d %b %Y}" if m.sent_at else m.state
                lines.append(f"- [{when}] {_one_line(m.subject, 120)} (by {m.owner_name or 'team'})"
                             + (f" - reply: {m.reply_status.replace('_', ' ')}" if m.reply_status else ""))
                if m.id in bodies:
                    lines.append("  " + _one_line(bodies[m.id], 300))
        if p.replies:
            lines.append("")
            lines.append("Their replies (their words, quoted):")
            for r in p.replies[:6]:
                lines.append(f"- {r.received_at:%d %b %Y} {r.kind}{('/' + r.intent) if r.intent else ''}: \"{_one_line(r.snippet, 300)}\"")
        open_fu = [f for f in p.follow_ups if not f.done]
        if open_fu:
            lines.append("")
            lines.append("Open follow-ups:")
            for f in open_fu[:8]:
                lines.append(f"- {f.due_on:%d %b %Y} {f.kind_label}" + (f": {_one_line(f.note, 200)}" if f.note else "")
                             + (" (overdue)" if f.overdue else ""))
        events = [t for t in p.timeline if t.kind not in ("sheet_updated",)][:20]
        if events:
            lines.append("")
            lines.append("Timeline (newest first):")
            for t in events:
                lines.append(f"- {t.occurred_at:%d %b %Y} {t.kind_label}" + (f": {_one_line(t.body, 200)}" if t.body else "")
                             + f" ({t.actor})")
    if linkedin_text:
        lines.append("")
        lines.append("LinkedIn company page text (fetched by our laptop worker; their words, quoted):")
        lines.append(_block(linkedin_text, 3000))
    text = "\n".join(lines).strip()
    return _block(text, 20_000 if full else 7_000)


__all__ = ["Basics", "basics", "build"]

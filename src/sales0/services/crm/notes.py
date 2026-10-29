"""Our own notes on a prospect: dated, authored entries (``prospect_notes``).

* Any team member adds a note; only its AUTHOR edits or deletes it (admins too: the same rule as outreach
  messages - the owner's words are the owner's). A note without an author (migrated from the old single
  ``prospects.notes`` field) may be edited / deleted by an admin.
* The sheet never touches these rows: the sheet's own notes column lives in ``prospects.sheet_notes`` (read-only,
  shown next to them), so a sheet sync cannot clobber what the team wrote.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import Prospect, ProspectNote, TeamMember
from sales0.errors import DomainError
from sales0.services._common import now_or
from sales0.services.crm.actor import Actor

MAX_NOTE_CHARS = 4000


class NoteView(BaseModel):
    id: int
    prospect_id: int
    body: str
    author: str
    author_member_id: int | None = None
    source: str = "app"
    created_at: datetime
    updated_at: datetime
    edited: bool = False
    can_edit: bool = False


def _clean(body: str | None) -> str:
    text = (body or "").replace("\r\n", "\n").strip()
    if not text:
        raise DomainError("note_invalid", "Write something before saving the note.", 400)
    if len(text) > MAX_NOTE_CHARS:
        raise DomainError("note_invalid", f"A note is at most {MAX_NOTE_CHARS} characters.", 400)
    return text


def _prospect(db: Session, prospect_id: int) -> Prospect:
    p = db.get(Prospect, prospect_id)
    if p is None:
        raise DomainError("not_found", "That prospect no longer exists - refresh the page.", 404)
    return p


def may_edit(note: ProspectNote, member: TeamMember | None) -> bool:
    if member is None:
        return False
    if note.author_member_id is not None:
        return note.author_member_id == member.id
    return member.role == "admin"


def view(note: ProspectNote, member: TeamMember | None = None) -> NoteView:
    return NoteView(
        id=note.id, prospect_id=note.prospect_id, body=note.body, author=note.author, author_member_id=note.author_member_id,
        source=note.source, created_at=note.created_at, updated_at=note.updated_at,
        edited=(note.updated_at - note.created_at).total_seconds() > 1, can_edit=may_edit(note, member),
    )  # fmt: skip


def list_notes(db: Session, prospect_id: int, member: TeamMember | None = None) -> list[NoteView]:
    rows = db.scalars(
        select(ProspectNote).where(ProspectNote.prospect_id == prospect_id).order_by(ProspectNote.created_at.desc(), ProspectNote.id.desc())
    ).all()
    return [view(n, member) for n in rows]


def add_note(
    db: Session, prospect_id: int, body: str, *, actor: Actor, member: TeamMember | None = None, now: datetime | None = None
) -> NoteView:
    _prospect(db, prospect_id)
    stamp = now_or(now)
    row = ProspectNote(
        prospect_id=prospect_id, body=_clean(body), author_member_id=actor.member_id, author=actor.label[:80],
        source="app", created_at=stamp, updated_at=stamp,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return view(row, member)


def _own(db: Session, note_id: int, member: TeamMember | None) -> ProspectNote:
    row = db.get(ProspectNote, note_id)
    if row is None:
        raise DomainError("not_found", "That note no longer exists - refresh the page.", 404)
    if not may_edit(row, member):
        raise DomainError("not_owner", "Only the person who wrote a note can change or delete it.", 403)
    return row


def edit_note(db: Session, note_id: int, body: str, *, member: TeamMember | None, now: datetime | None = None) -> NoteView:
    row = _own(db, note_id, member)
    text = _clean(body)
    if text != row.body:
        row.body = text
        row.updated_at = now_or(now)
        db.flush()
    return view(row, member)


def delete_note(db: Session, note_id: int, *, member: TeamMember | None) -> int:
    """Delete one of your own notes; returns its prospect id."""
    row = _own(db, note_id, member)
    pid = row.prospect_id
    db.delete(row)
    db.flush()
    return pid


def latest_texts(db: Session, prospect_id: int, limit: int = 20) -> list[tuple[datetime, str, str]]:
    """(created_at, author, body) newest first - the AI context reads these."""
    rows = db.scalars(
        select(ProspectNote).where(ProspectNote.prospect_id == prospect_id).order_by(ProspectNote.created_at.desc()).limit(limit)
    ).all()
    return [(n.created_at, n.author, n.body) for n in rows]


__all__ = ["NoteView", "add_note", "delete_note", "edit_note", "latest_texts", "list_notes", "may_edit"]

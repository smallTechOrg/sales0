"""Message templates: placeholders, safe rendering, validation and CRUD.

Variables: {first_name} {company} {requirement} {sender_name} {offering}. ``{{name}}`` is read
as the same placeholder. Rendering never raises: a placeholder with no value (or an unknown
one) stays in the text and is reported, so Approve can be blocked while any remain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.db.models import MessageTemplate
from sales0.errors import DomainError
from sales0.services._common import not_found, now_or

VARIABLES: tuple[str, ...] = ("first_name", "company", "requirement", "sender_name", "offering")

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}|\{([A-Za-z_]\w*)\}")
# "[Your Name]" / "[Company]" style gaps that models like to leave behind.
_BRACKET_GAP = re.compile(r"\[(?:your|insert|name|company|first|recipient|sender|add|date|link)[^\]\n]{0,40}\]", re.I)


def find_placeholders(text: str) -> list[str]:
    """Every placeholder-looking token in ``text`` (names for {x}/{{x}}, the literal for [Your Name]), in order, unique."""
    found: list[str] = []
    for m in _PLACEHOLDER.finditer(text or ""):
        name = m.group(1) or m.group(2)
        if name not in found:
            found.append(name)
    for m in _BRACKET_GAP.finditer(text or ""):
        if m.group(0) not in found:
            found.append(m.group(0))
    if "{{" in (text or "") and not _PLACEHOLDER.search(text):
        found.append("{{")
    return found


@dataclass(frozen=True)
class Rendered:
    text: str
    unknown: list[str] = field(default_factory=list)  # placeholders that are not template variables
    unfilled: list[str] = field(default_factory=list)  # known variables that had no value

    @property
    def ok(self) -> bool:
        return not self.unknown and not self.unfilled


def render(text: str, values: dict[str, str | None]) -> Rendered:
    """Substitute known variables that have a non-blank value; leave everything else in place."""
    unknown: list[str] = []
    unfilled: list[str] = []

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name not in VARIABLES:
            if name not in unknown:
                unknown.append(name)
            return m.group(0)
        value = (values.get(name) or "").strip()
        if not value:
            if name not in unfilled:
                unfilled.append(name)
            return m.group(0)
        return value

    return Rendered(_PLACEHOLDER.sub(sub, text or ""), unknown, unfilled)


def template_values(
    *, first_name: str | None, company: str | None, requirement: str | None, sender_name: str, offering: str | None
) -> dict[str, str | None]:
    return {
        "first_name": first_name,
        "company": company,
        "requirement": requirement,
        "sender_name": sender_name,
        "offering": offering,
    }


def unknown_placeholders(*texts: str) -> list[str]:
    out: list[str] = []
    for text in texts:
        for m in _PLACEHOLDER.finditer(text or ""):
            name = m.group(1) or m.group(2)
            if name not in VARIABLES and name not in out:
                out.append(name)
    return out


def validate_template(name: str, subject: str, body: str) -> list[str]:
    """Problems with a template definition (empty list = valid)."""
    problems: list[str] = []
    if not name.strip() or len(name.strip()) > 120:
        problems.append("Give the template a name (1-120 characters).")
    if len(subject) > 300:
        problems.append("The subject is longer than 300 characters.")
    if not body.strip():
        problems.append("The body is empty.")
    bad = unknown_placeholders(subject, body)
    if bad:
        allowed = ", ".join("{" + v + "}" for v in VARIABLES)
        problems.append(f"Unknown placeholder(s): {', '.join('{' + b + '}' for b in bad)}. Allowed: {allowed}.")
    return problems


def _check(name: str, subject: str, body: str) -> None:
    problems = validate_template(name, subject, body)
    if problems:
        raise DomainError("template_invalid", " ".join(problems), 400)


def create_template(
    db: Session, *, name: str, subject: str, body: str, offering_id: int | None = None, now: datetime | None = None
) -> MessageTemplate:
    name, subject = " ".join(name.split()), subject.strip()
    _check(name, subject, body)
    stamp = now_or(now)
    row = MessageTemplate(
        offering_id=offering_id, name=name, subject=subject, body=body.strip(), active=True,
        created_at=stamp, updated_at=stamp,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def get_template(db: Session, template_id: int) -> MessageTemplate:
    row = db.get(MessageTemplate, template_id)
    if row is None:
        raise not_found()
    return row


def update_template(
    db: Session,
    template_id: int,
    *,
    name: str,
    subject: str,
    body: str,
    offering_id: int | None = None,
    now: datetime | None = None,
) -> MessageTemplate:
    row = get_template(db, template_id)
    name, subject = " ".join(name.split()), subject.strip()
    _check(name, subject, body)
    row.name, row.subject, row.body, row.offering_id = name, subject, body.strip(), offering_id
    row.updated_at = now_or(now)
    db.flush()
    return row


def delete_template(db: Session, template_id: int) -> None:
    """Soft delete (existing messages keep their template_id)."""
    row = get_template(db, template_id)
    row.active = False
    db.flush()


def list_templates(db: Session, *, offering_id: int | None = None, include_inactive: bool = False) -> list[MessageTemplate]:
    stmt = select(MessageTemplate).order_by(MessageTemplate.name, MessageTemplate.id)
    if offering_id is not None:
        stmt = stmt.where(MessageTemplate.offering_id == offering_id)
    if not include_inactive:
        stmt = stmt.where(MessageTemplate.active.is_(True))
    return list(db.execute(stmt).scalars())


def template_for_offering(db: Session, offering_id: int | None) -> MessageTemplate | None:
    """The newest active template for the offering, else the newest generic (no offering) one."""
    if offering_id is not None:
        row = db.execute(
            select(MessageTemplate)
            .where(MessageTemplate.offering_id == offering_id, MessageTemplate.active.is_(True))
            .order_by(MessageTemplate.id.desc())
        ).scalars().first()
        if row is not None:
            return row
    return db.execute(
        select(MessageTemplate)
        .where(MessageTemplate.offering_id.is_(None), MessageTemplate.active.is_(True))
        .order_by(MessageTemplate.id.desc())
    ).scalars().first()

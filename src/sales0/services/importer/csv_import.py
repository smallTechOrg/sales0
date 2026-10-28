"""CSV import of app-owned data (``origin = import``).

* The sheet kinds (enquiries, discarded, outreach, prospects, working, competitors, groups) are parsed
  by the SAME parser as the synced sheet tabs (``sheetsync.parsers.parse_tab``) and applied with the
  SAME mapping (``sheetsync.mapping.apply_record``, origin ``import``, no ``sheet_sources`` row), so
  every quirk behaves identically. ``clients`` and ``contacts`` use the small local parsers below.
* Sheet wins: a record whose natural key already belongs to a sheet-fed row is counted ``merged`` and
  nothing is written. Re-importing a file is idempotent (second run: new 0, unchanged n).
* Input hygiene: BOM / UTF-16 / latin-1 are decoded, the delimiter (``, ; TAB |``) is sniffed, an Excel
  workbook is refused with a hint, at most 5 MB and 20 000 data rows.
* A bad row never stops the import: it is reported as ``(row number, code)``; reports never carry cell
  content. Every row is applied in its own savepoint, so a failing row leaves no partial write.
* ``dry_run=True`` (or :func:`preview_csv`) runs the whole import inside a savepoint that is rolled
  back, and returns the same counts with ``id=None``.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.clock import local_today
from sales0.config import get_settings
from sales0.db.models import (
    Client,
    Contact,
    ContactEmail,
    ContactPhone,
    ContactTag,
    SheetSource,
    Tag,
)
from sales0.errors import DomainError
from sales0.logging import get_logger
from sales0.services._common import now_or
from sales0.services.crm import clients, companies, contacts, flags, timeline
from sales0.services.crm.actor import IMPORT, Actor
from sales0.services.importer.batches import record_batch
from sales0.services.importer.views import ImportBatchView, ImportIssueView
from sales0.services.sheetsync import mapping
from sales0.services.sheetsync import values as v
from sales0.services.sheetsync.parsers import _build_colmap, norm_header, parse_tab, read_csv
from sales0.services.sheetsync.tabs import TabSpec

log = get_logger("sales0.importer.csv")

SHEET_KINDS = ("enquiries", "discarded", "outreach", "prospects", "working", "competitors", "groups")
KINDS = (*SHEET_KINDS, "clients", "contacts")
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
DELIMITERS = ",;\t|"
INVALID_MESSAGE = "Choose a CSV file and what it contains."

CLIENT_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "client", "client name", "customer"),
    "company": ("company", "organisation", "organization", "company name"),
    "started_on": ("started on", "started_on", "start date", "started", "since", "client since"),
    "notes": ("notes", "note", "comments", "remarks"),
}
CONTACT_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "full name", "contact", "contact name", "contact person"),
    "email": ("email", "e-mail", "email address", "emails", "e-mail address"),
    "phone": ("phone", "mobile", "phone number", "contact number", "mobile number"),
    "linkedin": ("linkedin", "linkedin url", "linkedin profile", "linkedin link"),
    "company": ("company", "organisation", "organization", "company name"),
    "tags": ("tags", "tag", "labels"),
    "title": ("title", "designation", "job title"),
}
LOCAL_ALIASES = {"clients": CLIENT_ALIASES, "contacts": CONTACT_ALIASES}
LOCAL_IDENTITY = {"clients": ("name",), "contacts": ("name", "email", "linkedin")}
_TAG_SPLIT = re.compile(r"[;,|]")


# ------------------------------------------------------------------------------ input handling
def decode(data: bytes) -> str:
    """UTF-8 (with or without BOM), UTF-16 (with BOM), else latin-1 (never fails)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def sniff_delimiter(text: str) -> str:
    """The delimiter that splits the first non-empty lines most consistently (default comma)."""
    lines = [ln for ln in text.splitlines()[:12] if ln.strip()]
    if not lines:
        return ","
    best, best_score = ",", 0
    for d in DELIMITERS:
        counts = [len(next(csv.reader([ln], delimiter=d), [])) - 1 for ln in lines]
        score = min(counts) if counts and min(counts) > 0 else 0
        if score > best_score:
            best, best_score = d, score
    return best


def _normalised_csv(text: str) -> tuple[str, list[list[str]]]:
    """Comma-delimited text (what the sheet parser reads) plus the parsed rows."""
    delim = sniff_delimiter(text)
    if delim == ",":
        return text, read_csv(text)
    csv.field_size_limit(2**31 - 1)
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    out = io.StringIO(newline="")
    csv.writer(out).writerows(rows)
    return out.getvalue(), rows


def _check_input(filename: str, kind: str, data: bytes) -> None:
    if kind not in KINDS or not isinstance(data, bytes | bytearray) or not data.strip():
        raise DomainError("import_invalid", INVALID_MESSAGE, 400)
    if len(data) > MAX_BYTES:
        raise DomainError("import_too_large", "That file is over 5 MB - split it and import the parts.", 413)
    if data[:4] == b"PK\x03\x04" or data[:4] == b"\xd0\xcf\x11\xe0":
        raise DomainError("import_invalid", "That looks like an Excel file - save it as CSV first.", 400)


# ------------------------------------------------------------------------------ outcomes
@dataclass
class _Tally:
    new: int = 0
    merged: int = 0
    unchanged: int = 0
    error: int = 0
    total: int = 0

    def add(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, DomainError):
        return exc.code
    return f"apply-failed:{type(exc).__name__}"


# ------------------------------------------------------------------------------ sheet kinds
def _sheet_fed(db: Session, entity: str, key: str) -> bool:
    return db.scalar(select(SheetSource.id).where(SheetSource.entity == entity, SheetSource.key == key).limit(1)) is not None


_ACTION_TO_OUTCOME = {"created": "new", "updated": "merged", "restored": "merged", "unchanged": "unchanged", "merged": "merged"}  # noqa: E501


def _import_sheet_kind(
    db: Session, kind: str, filename: str, text: str, rows: list[list[str]], tally: _Tally, issues: list[ImportIssueView],
    ignored: list[str], now: datetime,
) -> bool:  # fmt: skip
    """Returns True when an enquiry was created or changed."""
    settings = get_settings()
    parsed = parse_tab(text, TabSpec(filename or "import", kind, "import", "0"), today=local_today(settings.tz, now))
    if parsed.fatal:
        raise DomainError(
            "import_invalid", "The header row was not recognised for this kind of file - check the columns.", 400
        )
    if parsed.header_row:
        header = rows[parsed.header_row - 1] if parsed.header_row - 1 < len(rows) else []
        colmap, _ = _build_colmap(header, kind)
        used = set(colmap.values())
        ignored.extend(_ignored(header, used))
    tally.total = parsed.data_rows
    for e in parsed.errors:
        tally.add("error")
        issues.append(ImportIssueView(e.row, e.code, "error"))
    warns = [ImportIssueView(w.row, w.code, "warning") for w in parsed.warnings]
    touched = False
    for pr in parsed.rows:
        rec = pr.record
        try:
            with db.begin_nested():
                if _sheet_fed(db, rec.ENTITY, rec.key):
                    outcome = "merged"  # sheet wins: nothing written
                else:
                    res = mapping.apply_record(db, rec, origin="import", source=None, actor=IMPORT, now=now)
                    outcome = _ACTION_TO_OUTCOME.get(res.action, "merged")
                    if outcome != "unchanged" and res.target_type == "enquiry":
                        touched = True
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the import
            tally.add("error")
            issues.append(ImportIssueView(pr.row_number, _error_code(exc), "error"))
            continue
        tally.add(outcome)
    issues.extend(warns)
    return touched


def _ignored(header: Sequence[str], used: set[int]) -> list[str]:
    names = [norm_header(h)[:60] for i, h in enumerate(header) if i not in used and norm_header(h)]
    return list(dict.fromkeys(names))[:50]


# ------------------------------------------------------------------------------ local kinds
@dataclass
class _LocalRow:
    number: int
    cells: dict[str, str]


def _parse_local(kind: str, rows: list[list[str]], tally: _Tally, issues: list[ImportIssueView], ignored: list[str]) -> list[_LocalRow]:  # noqa: E501
    aliases = LOCAL_ALIASES[kind]
    lookup = {a: f for f, names in aliases.items() for a in names}
    identity = LOCAL_IDENTITY[kind]
    header_idx = colmap = None
    for i, row in enumerate(rows[:10]):
        cm: dict[str, int] = {}
        for j, h in enumerate(row):
            f = lookup.get(norm_header(h))
            if f and f not in cm:
                cm[f] = j
        if any(f in cm for f in identity):
            header_idx, colmap = i, cm
            break
    if header_idx is None or colmap is None:
        raise DomainError(
            "import_invalid", "The header row was not recognised for this kind of file - check the columns.", 400
        )
    ignored.extend(_ignored(rows[header_idx], set(colmap.values())))
    out: list[_LocalRow] = []
    for offset, raw in enumerate(rows[header_idx + 1 :]):
        number = header_idx + 2 + offset
        if not any(c.strip() for c in raw):
            continue
        cells: dict[str, str] = {}
        for f, idx in colmap.items():
            txt, truncated = v.clean_text(raw[idx], max_chars=5000) if idx < len(raw) else ("", False)
            cells[f] = "" if v.is_null(txt) else txt.strip()
            if truncated:
                issues.append(ImportIssueView(number, "cell-truncated", "warning"))
        tally.total += 1
        if not any(cells.get(f) for f in identity):
            tally.add("error")
            issues.append(ImportIssueView(number, "no-identifying-content", "error"))
            continue
        out.append(_LocalRow(number, cells))
    return out


def _squash(text: str) -> str:
    return " ".join(text.split())


def _import_clients(db: Session, rows: list[_LocalRow], tally: _Tally, issues: list[ImportIssueView], now: datetime) -> None:  # noqa: E501
    today = local_today(get_settings().tz, now)
    for r in rows:
        name = _squash(r.cells.get("name", ""))
        if not name:
            tally.add("error")
            issues.append(ImportIssueView(r.number, "no-identifying-content", "error"))
            continue
        company = _squash(r.cells.get("company", "")) or None
        started: date | None = None
        raw_date = r.cells.get("started_on", "")
        if raw_date:
            started = v.parse_date(raw_date, ref=today)
            if started is None:
                issues.append(ImportIssueView(r.number, "bad-date:started_on", "warning"))
        try:
            with db.begin_nested():
                company_id = companies.get_or_create(db, company).id if company else None
                stmt = select(Client).where(func.lower(Client.name) == name.casefold())
                stmt = stmt.where(Client.company_id.is_(None) if company_id is None else Client.company_id == company_id)
                existing = db.scalars(stmt.order_by(Client.id).limit(1)).first()
                if existing is not None:
                    outcome = "unchanged" if existing.source == "legacy_import" else "merged"
                else:
                    clients.create_legacy(
                        db, name=name, company=company, started_on=started, notes=r.cells.get("notes") or None,
                        actor=IMPORT, now=now,
                    )  # fmt: skip
                    outcome = "new"
        except Exception as exc:  # noqa: BLE001
            tally.add("error")
            issues.append(ImportIssueView(r.number, _error_code(exc), "error"))
            continue
        tally.add(outcome)


def _contact_state(db: Session, contact_id: int) -> tuple[Any, ...]:
    c = db.get(Contact, contact_id)
    assert c is not None
    emails = sorted(db.scalars(select(ContactEmail.email).where(ContactEmail.contact_id == contact_id)).all())
    phones = sorted(db.scalars(select(ContactPhone.digits).where(ContactPhone.contact_id == contact_id)).all())
    tags = sorted(
        db.scalars(
            select(Tag.name_key).join(ContactTag, ContactTag.tag_id == Tag.id).where(ContactTag.contact_id == contact_id)
        ).all()
    )
    return (c.full_name, c.title, c.company_id, c.linkedin_slug, c.email, c.phone, tuple(emails), tuple(phones), tuple(tags))  # noqa: E501


def _find_contact(db: Session, emails: Sequence[str], slug: str | None) -> int | None:
    for e in emails:
        cid = db.scalar(select(ContactEmail.contact_id).where(ContactEmail.email == e.casefold()))
        if cid is not None:
            return int(cid)
    if slug:
        cid = db.scalar(select(Contact.id).where(Contact.linkedin_slug == slug))
        if cid is not None:
            return int(cid)
    return None


def _import_contacts(db: Session, rows: list[_LocalRow], tally: _Tally, issues: list[ImportIssueView], now: datetime) -> None:  # noqa: E501
    for r in rows:
        c = r.cells
        emails = list(v.parse_emails(c.get("email")))
        if c.get("email") and not emails:
            issues.append(ImportIssueView(r.number, "bad-email", "warning"))
        phones = list(v.parse_phones(c.get("phone")))
        if c.get("phone") and not phones:
            issues.append(ImportIssueView(r.number, "bad-phone", "warning"))
        linkedin = c.get("linkedin") or None
        slug = v.linkedin_slug(linkedin) if linkedin else None
        if linkedin and not slug:
            issues.append(ImportIssueView(r.number, "bad-linkedin", "warning"))
            linkedin = None
        name = _squash(c.get("name", "")) or None
        if not (name or emails or slug):
            tally.add("error")
            issues.append(ImportIssueView(r.number, "no-identifying-content", "error"))
            continue
        try:
            with db.begin_nested():
                outcome = _upsert_contact(db, r, name, emails, phones, linkedin, slug, issues, now)
        except Exception as exc:  # noqa: BLE001
            tally.add("error")
            issues.append(ImportIssueView(r.number, _error_code(exc), "error"))
            continue
        tally.add(outcome)


def _upsert_contact(
    db: Session, r: _LocalRow, name: str | None, emails: list[str], phones: list[str], linkedin: str | None,
    slug: str | None, issues: list[ImportIssueView], now: datetime,
) -> str:  # fmt: skip
    existing_id = _find_contact(db, emails, slug)
    before = _contact_state(db, existing_id) if existing_id is not None else None
    count_before = db.scalar(select(func.count(Contact.id))) or 0
    company_name = _squash(r.cells.get("company", "")) or None
    company_id = companies.get_or_create(db, company_name).id if company_name else None
    lookup_email = next((e for e in emails if _find_contact(db, [e], None) is not None), emails[0] if emails else None)
    view = contacts.upsert(
        db, full_name=name, email=lookup_email, phone=phones[0] if phones else None, linkedin_url=linkedin,
        title=r.cells.get("title") or None, company_id=company_id, origin="import", actor=IMPORT, now=now,
    )  # fmt: skip
    cid = int(view.id)
    for extra in emails:  # every address of the cell is kept
        if db.scalar(select(ContactEmail.id).where(ContactEmail.email == extra.casefold())) is None:
            db.add(ContactEmail(contact_id=cid, email=extra.casefold(), is_primary=False))
    for phone in phones:
        digits = re.sub(r"\D", "", phone)[-10:]
        if digits and db.scalar(select(ContactPhone.id).where(ContactPhone.contact_id == cid, ContactPhone.digits == digits)) is None:  # noqa: E501
            db.add(ContactPhone(contact_id=cid, phone=phone, digits=digits))
    db.flush()
    have = set(
        db.scalars(select(Tag.name_key).join(ContactTag, ContactTag.tag_id == Tag.id).where(ContactTag.contact_id == cid)).all()
    )
    for tag in (t.strip() for t in _TAG_SPLIT.split(r.cells.get("tags", "")) if t.strip()):
        if _squash(tag).casefold() in have:
            continue
        try:
            with db.begin_nested():
                contacts.add_tag(db, cid, tag, "custom", actor=IMPORT, now=now)
        except DomainError:
            issues.append(ImportIssueView(r.number, "tag-invalid", "warning"))
    count_after = db.scalar(select(func.count(Contact.id))) or 0
    if count_after > count_before:
        timeline.add_event(
            db, subject_type="contact", subject_id=cid, kind="import", body="Imported from a CSV file", actor=IMPORT,
            occurred_at=now, contact_id=cid,
        )  # fmt: skip
        return "new"
    if before is not None and before == _contact_state(db, cid):
        return "unchanged"
    return "merged"


# ------------------------------------------------------------------------------ entry points
def _run(db: Session, *, filename: str, kind: str, data: bytes, actor: Actor, now: datetime, dry_run: bool) -> ImportBatchView:  # noqa: E501
    _check_input(filename, kind, data)
    text, rows = _normalised_csv(decode(bytes(data)))
    if len(rows) > MAX_ROWS + 10:
        raise DomainError("import_too_large", "That file has more than 20 000 rows - split it and import the parts.", 413)
    tally = _Tally()
    issues: list[ImportIssueView] = []
    ignored: list[str] = []
    touched_enquiries = False
    savepoint = db.begin_nested()
    try:
        if kind in SHEET_KINDS:
            touched_enquiries = _import_sheet_kind(db, kind, filename, text, rows, tally, issues, ignored, now)
        else:
            parsed = _parse_local(kind, rows, tally, issues, ignored)
            if len(parsed) > MAX_ROWS:
                raise DomainError("import_too_large", "That file has more than 20 000 rows - split it.", 413)
            (_import_clients if kind == "clients" else _import_contacts)(db, parsed, tally, issues, now)
        if touched_enquiries and not dry_run:
            flags.recompute_all(db, now=now)
        issues.sort(key=lambda i: (i.level != "error", i.row))
        issues.extend(ImportIssueView(0, f"ignored-column:{n}", "warning") for n in ignored)
        batch_id: int | None = None
        created_at: datetime | None = None
        if dry_run:
            savepoint.rollback()
        else:
            row = record_batch(
                db, filename=filename, kind=kind, actor=actor,
                counts=(tally.total, tally.new, tally.merged, tally.unchanged, tally.error), issues=issues, now=now,
            )  # fmt: skip
            batch_id, created_at = row.id, row.created_at
            savepoint.commit()
    except BaseException:
        if savepoint.is_active:
            savepoint.rollback()
        raise
    shown = [i for i in issues if not i.code.startswith("ignored-column:")]
    return ImportBatchView(
        id=batch_id, filename=filename, kind=kind, rows_total=tally.total, rows_new=tally.new,
        rows_merged=tally.merged, rows_unchanged=tally.unchanged, rows_error=tally.error, errors=shown,
        ignored_columns=ignored, dry_run=dry_run, actor=actor.label, created_at=created_at,
    )  # fmt: skip


def import_csv(db: Session, *, filename: str, kind: str, data: bytes, actor: Actor, now: datetime | None = None, dry_run: bool = False) -> ImportBatchView:  # noqa: E501
    """Import one CSV file; writes an ``import_batches`` row (not when ``dry_run``). Flushes, never commits."""
    name = (filename or "import.csv").replace("\\", "/").rsplit("/", 1)[-1][:255] or "import.csv"
    return _run(db, filename=name, kind=kind, data=data, actor=actor, now=now_or(now), dry_run=dry_run)


def preview_csv(db: Session, *, filename: str, kind: str, data: bytes, actor: Actor = IMPORT, now: datetime | None = None) -> ImportBatchView:  # noqa: E501
    """Dry run: the counts and the per-row report an import would give, with nothing written."""
    return import_csv(db, filename=filename, kind=kind, data=data, actor=actor, now=now, dry_run=True)


__all__ = ["KINDS", "MAX_BYTES", "MAX_ROWS", "SHEET_KINDS", "decode", "import_csv", "preview_csv", "sniff_delimiter"]

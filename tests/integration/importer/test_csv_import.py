"""CSV import: counts, idempotency, encodings/delimiters, dry run, per-row errors, sheet wins, no data loss."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales0.db.engine import get_sessionmaker
from sales0.db.models import (
    Client,
    Competitor,
    Contact,
    ContactEmail,
    ContactTag,
    Enquiry,
    ImportBatch,
    Tag,
    TimelineEvent,
)
from sales0.errors import DomainError
from sales0.services import importer
from sales0.services.crm.actor import IMPORT, TEAM
from sales0.services.importer import csv_import
from tests.integration.importer.conftest import fixture_bytes

# Rows of enquiries_messy.csv: 2 Alpha (ok) | 3 Beta (two bad dates, "Not provided" e-mail) | 4-5 template
# rows (only Status/Priority: skipped as blank) | 6 ragged row with no identifying content | 7 Gamma (2 e-mails).


def count(db: Session, model: type) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def run(db: Session, kind: str, data: bytes, name: str = "f.csv", **kw: object) -> importer.ImportBatchView:
    view = importer.import_csv(db, filename=name, kind=kind, data=data, actor=IMPORT, **kw)  # type: ignore[arg-type]
    db.commit()
    return view


def counts(v: importer.ImportBatchView) -> tuple[int, int, int, int, int]:
    return (v.rows_total, v.rows_new, v.rows_merged, v.rows_unchanged, v.rows_error)


# ------------------------------------------------------------------------------ enquiries
def test_120_row_enquiries_import_then_reimport_is_idempotent(db: Session) -> None:
    data = fixture_bytes("enquiries_120.csv")
    first = run(db, "enquiries", data)
    assert counts(first) == (120, 120, 0, 0, 0) and first.id is not None and first.errors == []
    assert count(db, Enquiry) == 120
    assert db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.origin == "import")) == 120
    events = count(db, TimelineEvent)

    second = run(db, "enquiries", data)
    assert counts(second) == (120, 0, 0, 120, 0)
    assert count(db, Enquiry) == 120 and count(db, TimelineEvent) == events  # nothing changed, nothing logged
    assert count(db, ImportBatch) == 2


def test_batch_row_is_stored_and_readable(db: Session) -> None:
    view = run(db, "enquiries", fixture_bytes("enquiries_messy.csv"), name="C:\\fakepath\\my export.csv")
    stored = importer.get_batch(db, view.id)  # type: ignore[arg-type]
    assert stored.filename == "my export.csv" and stored.kind == "enquiries" and stored.actor == "import"
    assert counts(stored) == counts(view)
    assert [b.id for b in importer.list_batches(db)] == [view.id]
    with pytest.raises(DomainError) as e:
        importer.get_batch(db, 9999)
    assert e.value.code == "not_found"


def test_messy_file_reports_row_numbers_and_codes_and_imports_the_rest(db: Session) -> None:
    v = run(db, "enquiries", fixture_bytes("enquiries_messy.csv"))
    assert counts(v) == (4, 3, 0, 0, 1)
    assert v.rows_total == v.rows_new + v.rows_merged + v.rows_unchanged + v.rows_error
    errors = [(i.row, i.code) for i in v.errors if i.level == "error"]
    assert errors == [(6, "no-identifying-content")]
    warnings = {(i.row, i.code) for i in v.errors if i.level == "warning"}
    assert (3, "bad-date:date_added") in warnings and (3, "bad-date:date_posted") in warnings
    assert v.ignored_columns == ["internal tag"]
    beta = db.scalars(select(Enquiry).where(Enquiry.company == "Beta Ltd")).one()
    assert beta.date_posted is None  # the unparseable date is empty, the row is kept
    stored = importer.get_batch(db, v.id)  # type: ignore[arg-type]
    assert [(i.row, i.code) for i in stored.errors if i.level == "error"] == errors
    assert stored.ignored_columns == ["internal tag"]
    # reports carry codes and row numbers only, never cell content
    assert all("beta" not in i.code.lower() and "alpha" not in i.code.lower() for i in stored.errors)


def _variants() -> dict[str, bytes]:
    base = fixture_bytes("enquiries_messy.csv").decode()
    import csv
    import io

    rows = list(csv.reader(io.StringIO(base)))

    def delim(d: str) -> str:
        out = io.StringIO()
        csv.writer(out, delimiter=d, lineterminator="\n").writerows(rows)
        return out.getvalue()

    return {
        "utf8-bom": b"\xef\xbb\xbf" + base.encode(),
        "utf16": base.encode("utf-16"),
        "latin1": base.replace("Gamma Ltd", "Gamm\u00e4 Ltd").encode("latin-1"),
        "semicolon": delim(";").encode(),
        "tab": delim("\t").encode(),
        "pipe": delim("|").encode(),
        "crlf": base.replace("\r\n", "\n").replace("\n", "\r\n").encode(),
        "lf": base.replace("\r\n", "\n").encode(),
    }


@pytest.mark.parametrize("variant", sorted(_variants()))
def test_encodings_and_delimiters_give_the_same_result(db: Session, variant: str) -> None:
    v = run(db, "enquiries", _variants()[variant])
    assert counts(v) == (4, 3, 0, 0, 1), variant
    assert [i.row for i in v.errors if i.level == "error"] == [6]


def test_dry_run_reports_counts_and_writes_nothing(db: Session) -> None:
    data = fixture_bytes("enquiries_messy.csv")
    preview = importer.preview_csv(db, filename="m.csv", kind="enquiries", data=data)
    assert preview.dry_run and preview.id is None and counts(preview) == (4, 3, 0, 0, 1)
    db.commit()
    assert count(db, Enquiry) == 0 and count(db, ImportBatch) == 0 and count(db, TimelineEvent) == 0
    real = run(db, "enquiries", data)
    assert counts(real) == counts(preview) and [(i.row, i.code) for i in real.errors] == [
        (i.row, i.code) for i in preview.errors
    ]
    # a preview of an already imported file says "unchanged" and still writes nothing
    again = importer.preview_csv(db, filename="m.csv", kind="enquiries", data=data)
    assert counts(again) == (4, 0, 0, 3, 1)
    db.commit()
    assert count(db, ImportBatch) == 1 and count(db, Enquiry) == 3


def test_a_failing_row_is_reported_and_leaves_no_partial_write(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales0.services.sheetsync import mapping

    real = mapping.apply_record

    def flaky(db_: Session, record: object, **kw: object):  # type: ignore[no-untyped-def]
        if getattr(record, "company", None) == "Gamma Ltd":
            db_.add(Enquiry(content_hash="partial" * 8, raw_text="half written", origin="import"))
            db_.flush()
            raise RuntimeError("boom")
        return real(db_, record, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(mapping, "apply_record", flaky)
    v = run(db, "enquiries", fixture_bytes("enquiries_messy.csv"))
    assert counts(v) == (4, 2, 0, 0, 2)
    assert (7, "apply-failed:RuntimeError") in [(i.row, i.code) for i in v.errors]
    assert db.scalar(select(func.count()).select_from(Enquiry).where(Enquiry.raw_text == "half written")) == 0
    monkeypatch.setattr(mapping, "apply_record", real)
    fixed = run(db, "enquiries", fixture_bytes("enquiries_messy.csv"))
    assert counts(fixed) == (4, 1, 0, 2, 1)  # only the failed row is new; the others are unchanged


def test_crm_owned_edits_survive_a_reimport(db: Session) -> None:
    data = fixture_bytes("enquiries_messy.csv")
    run(db, "enquiries", data)
    alpha = db.scalars(select(Enquiry).where(Enquiry.company == "Alpha Ltd")).one()
    alpha.notes = "Called Asha, wants a quote"
    alpha.ai_reason = "kept by the AI"
    db.commit()
    again = run(db, "enquiries", data)
    assert counts(again) == (4, 0, 0, 3, 1)
    db.refresh(alpha)
    assert alpha.notes == "Called Asha, wants a quote" and alpha.ai_reason == "kept by the AI"


def test_sheet_fed_rows_win_and_are_never_overwritten(db: Session) -> None:
    from datetime import UTC, datetime

    from sales0.db.models import SheetSource
    from sales0.services.sheetsync.parsers import parse_tab
    from sales0.services.sheetsync.repository import SheetSource as SourceInfo
    from sales0.services.sheetsync.sql_repository import SqlSheetRepository
    from sales0.services.sheetsync.tabs import TabSpec

    sheet_csv = (
        "Website,Name,Approx no of clients,Notes\n"
        "https://importco.example.test,ImportCo (sheet),99,from the sheet\n"
    )
    now = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    tab = TabSpec("Competitors", "competitors", "sheetx", "945694185")
    (row,) = parse_tab(sheet_csv, tab, today=now.date()).rows
    SqlSheetRepository(get_sessionmaker()).upsert(
        row.record, SourceInfo("sheetx", "945694185", row.row_number, row.row_hash, "Competitors")
    )
    assert db.scalar(select(func.count()).select_from(SheetSource)) == 1

    v = run(db, "competitors", fixture_bytes("competitors_overlap.csv"))
    assert counts(v) == (2, 1, 1, 0, 0)  # ImportCo is sheet-fed -> merged; Rival Labs is new
    sheet_row = db.scalars(select(Competitor).where(Competitor.website_key.like("importco%"))).one()
    assert sheet_row.name == "ImportCo (sheet)" and sheet_row.approx_clients == 99
    assert db.scalar(select(func.count()).select_from(Competitor).where(Competitor.name == "Rival Labs")) == 1
    again = run(db, "competitors", fixture_bytes("competitors_overlap.csv"))
    assert counts(again) == (2, 0, 1, 1, 0)


# ------------------------------------------------------------------------------ clients / contacts
def test_clients_import_creates_legacy_clients_and_is_idempotent(db: Session) -> None:
    data = fixture_bytes("clients.csv")
    v = run(db, "clients", data)
    assert counts(v) == (4, 3, 0, 0, 1)
    assert [(i.row, i.code) for i in v.errors] == [(6, "no-identifying-content"), (4, "bad-date:started_on")]
    rows = db.scalars(select(Client).order_by(Client.id)).all()
    assert [c.name for c in rows] == ["Legacy One", "Legacy Two", "Legacy Three"]
    assert {c.status for c in rows} == {"legacy"} and {c.source for c in rows} == {"legacy_import"}
    assert rows[0].started_on is not None and rows[2].started_on is None
    again = run(db, "clients", data)
    assert counts(again) == (4, 0, 0, 3, 1) and count(db, Client) == 3


def test_contacts_import_keeps_every_email_and_tag_and_is_idempotent(db: Session) -> None:
    data = fixture_bytes("contacts.csv")
    v = run(db, "contacts", data)
    assert counts(v) == (4, 3, 0, 0, 1)
    assert (4, "no-identifying-content") in [(i.row, i.code) for i in v.errors]
    assert (5, "tag-invalid") in [(i.row, i.code) for i in v.errors]
    meera = db.scalars(select(Contact).where(Contact.full_name == "Meera Nair")).one()
    emails = set(db.scalars(select(ContactEmail.email).where(ContactEmail.contact_id == meera.id)))
    assert emails == {"meera@ctx.example.test", "meera.n@ctx.example.test"}
    assert meera.linkedin_slug == "in/meera-nair-ctx" and meera.origin == "import"
    tags = set(db.scalars(select(Tag.name).join(ContactTag, ContactTag.tag_id == Tag.id)))
    assert tags == {"buyer", "warm"}
    contacts_before = count(db, Contact)

    again = run(db, "contacts", data)
    assert counts(again) == (4, 0, 0, 3, 1)
    assert count(db, Contact) == contacts_before and count(db, ContactEmail) == 4  # 2 + 1 + 1


def test_contacts_reimport_enriches_an_existing_contact_without_blanking(db: Session) -> None:
    first = b"Name,Email,Phone\nRiya Shah,riya@ctx.example.test,\n"
    run(db, "contacts", first)
    second = b"Name,Email,Phone,Tags\n,riya@ctx.example.test,+91 98200 77777,vip\n"
    v = run(db, "contacts", second)
    assert counts(v) == (1, 0, 1, 0, 0)
    riya = db.scalars(select(Contact).where(Contact.email == "riya@ctx.example.test")).one()
    assert riya.full_name == "Riya Shah" and riya.phone is not None
    assert counts(run(db, "contacts", second)) == (1, 0, 0, 1, 0)


# ------------------------------------------------------------------------------ malformed input
@pytest.mark.parametrize(
    ("kind", "data", "code", "status"),
    [
        ("enquiries", b"", "import_invalid", 400),
        ("enquiries", b"  \n  ", "import_invalid", 400),
        ("nonsense", b"a,b\n1,2\n", "import_invalid", 400),
        ("enquiries", b"PK\x03\x04binary-workbook", "import_invalid", 400),
        ("enquiries", b"just,some,words\n1,2,3\n", "import_invalid", 400),
        ("clients", b"foo,bar\n1,2\n", "import_invalid", 400),
        ("contacts", b"foo,bar\n1,2\n", "import_invalid", 400),
        ("enquiries", b"x" * (5 * 1024 * 1024 + 1), "import_too_large", 413),
    ],
)
def test_malformed_files_are_refused_with_a_code_and_write_nothing(
    db: Session, kind: str, data: bytes, code: str, status: int
) -> None:
    with pytest.raises(DomainError) as e:
        importer.import_csv(db, filename="x.csv", kind=kind, data=data, actor=TEAM)
    assert (e.value.code, e.value.http_status) == (code, status)
    db.rollback()
    assert count(db, ImportBatch) == 0 and count(db, Enquiry) == 0


def test_more_than_20000_rows_is_refused(db: Session) -> None:
    body = "Name,Email\n" + "".join(f"P{i},p{i}@bulk.example.test\n" for i in range(csv_import.MAX_ROWS + 1))
    with pytest.raises(DomainError) as e:
        importer.import_csv(db, filename="big.csv", kind="contacts", data=body.encode(), actor=TEAM)
    assert e.value.code == "import_too_large"
    db.rollback()
    assert count(db, Contact) == 0


def test_garbage_bytes_never_crash_the_importer(db: Session) -> None:
    junk = bytes(range(256)) * 20
    with pytest.raises(DomainError) as e:
        importer.import_csv(db, filename="junk.csv", kind="enquiries", data=junk, actor=TEAM)
    assert e.value.code == "import_invalid"


def test_state_survives_a_new_session(db: Session) -> None:
    run(db, "enquiries", fixture_bytes("enquiries_120.csv"))
    with get_sessionmaker()() as other:
        assert count(other, Enquiry) == 120
        last = importer.list_batches(other)[0]
        assert counts(last) == (120, 120, 0, 0, 0)
        again = importer.import_csv(other, filename="e.csv", kind="enquiries", data=fixture_bytes("enquiries_120.csv"), actor=TEAM)
        assert counts(again) == (120, 0, 0, 120, 0)


# ---- QA regressions (synthetic) ---------------------------------------------------------------
def _csv(rows: list[list[str]]) -> bytes:
    from tests.unit.sheetsync import fixtures as fx

    return fx.to_csv([fx.ACTIVE_HEADER, *rows]).encode()


def _arow(n: int, **kw: str) -> list[str]:
    from tests.unit.sheetsync import fixtures as fx

    return fx.active_row(
        summary=f"Imp{n} - t - c", email=f"i{n}@x.example", link=f"https://www.linkedin.com/posts/a-activity-72000000000000{n:05d}-aa",
        company=f"Co{n}", full=f"unique enquiry text {n}", **kw,
    )


def test_reimporting_an_unchanged_csv_adds_no_timeline_events(db: Session) -> None:
    data = _csv([_arow(1, status="Email Sent"), _arow(2, status="Replied"), _arow(3, status="Bananas"), _arow(4, status="Won")])
    run(db, "enquiries", data)
    before = count(db, TimelineEvent)
    again = run(db, "enquiries", data)
    assert again.rows_unchanged == 4 and again.rows_new == 0
    assert count(db, TimelineEvent) == before


def test_rows_collapsed_by_content_hash_are_noted_in_the_batch(db: Session) -> None:
    same = "identical full enquiry text"
    from tests.unit.sheetsync import fixtures as fx

    def r(n: int) -> list[str]:
        return fx.active_row(summary=f"Dup{n}", email=f"d{n}@x.example", full=same,
                             link=f"https://www.linkedin.com/posts/a-activity-73000000000000{n:05d}-aa")

    view = run(db, "enquiries", _csv([r(1), r(2), _arow(9)]))
    codes = [i.code for i in view.errors]
    assert "duplicate-of-row:2" in codes and "rows-collapsed:1" in codes
    assert view.rows_total == view.rows_new + view.rows_merged + view.rows_unchanged + view.rows_error
    assert count(db, Enquiry) == 2


def test_read_upload_is_bounded() -> None:
    import io

    class Counting(io.BytesIO):
        total = 0

        def read(self, n: int = -1) -> bytes:
            out = super().read(n)
            Counting.total += len(out)
            return out

    big = Counting(b"x" * (csv_import.MAX_BYTES * 3))
    data = importer.read_upload(big)
    assert len(data) == csv_import.MAX_BYTES + 1 and Counting.total == csv_import.MAX_BYTES + 1
    with pytest.raises(DomainError) as exc:
        importer.import_csv(None, filename="b.csv", kind="enquiries", data=data, actor=IMPORT)  # type: ignore[arg-type]
    assert exc.value.code == "import_too_large"
    assert importer.read_upload(io.BytesIO(b"a,b\n1,2\n")) == b"a,b\n1,2\n"

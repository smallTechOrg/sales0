"""CRM feedback B3: an app edit of a sheet-fed contact / prospect company survives the next sync unless the sheet
cell itself changed (the audit #7 "overwrite only when the sheet cell changes" rule, now also for contact
e-mails / phones and the prospect's company, which used to be reassigned on every sync)."""

from __future__ import annotations

from sqlalchemy import select

from sales0.db.engine import session_scope
from sales0.db.models import Company, ContactEmail, ContactPhone, Enquiry, Prospect
from sales0.services.crm import contacts, prospects
from tests.unit.sheetsync import fixtures as fx

ACTIVE = fx.TABS[0]
OUTREACH = fx.TABS[2]


def _active(email: str = "a.one@acme.example, b.two@acme.example", notes: str = "first", phone: str = "+91 98765 43210, +91 90000 11111") -> dict[str, str]:
    row = fx.active_row(added="12 July", summary="Acme Corp - GenAI workshop - Pune | Online", email=email, priority="High",
                        status="New", notes=notes, company="Acme Corp", phone=phone, person="Asha Test",
                        link="https://www.linkedin.com/posts/x-activity-7000000000000000001-AbCd")
    return {"Active Enquiries": fx.active_csv([row])}


def _contact_id() -> int:
    with session_scope() as db:
        cid = db.scalar(select(Enquiry.contact_id).where(Enquiry.company == "Acme Corp"))
        assert cid is not None
        return cid


def _emails(cid: int) -> set[str]:
    with session_scope() as db:
        return set(db.scalars(select(ContactEmail.email).where(ContactEmail.contact_id == cid)))


def _digits(cid: int) -> set[str]:
    with session_scope() as db:
        return set(db.scalars(select(ContactPhone.digits).where(ContactPhone.contact_id == cid)))


def test_removed_email_and_phone_are_not_re_added_unless_the_cell_changes(sync) -> None:  # type: ignore[no-untyped-def]
    sync(_active(), tabs=(ACTIVE,))
    cid = _contact_id()
    assert _emails(cid) == {"a.one@acme.example", "b.two@acme.example"}
    assert {"9876543210", "9000011111"} <= _digits(cid)
    with session_scope() as db:
        detail = contacts.get_contact(db, cid)
        contacts.remove_email(db, cid, next(e.id for e in detail.email_items if e.email == "b.two@acme.example"))
        contacts.remove_phone(db, cid, next(p.id for p in detail.phone_items if p.phone.endswith("9000011111")))
    # another cell of the row changes (the row is re-applied) but not the e-mail / phone cells
    sync(_active(notes="second"), tabs=(ACTIVE,))
    assert _emails(cid) == {"a.one@acme.example"}
    assert "9000011111" not in _digits(cid)
    # the e-mail cell changes: only the address the sheet newly lists is added
    sync(_active(email="a.one@acme.example, b.two@acme.example, c.three@acme.example", notes="second"), tabs=(ACTIVE,))
    assert _emails(cid) == {"a.one@acme.example", "c.three@acme.example"}


def _outreach(company: str = "Pat Co", notes: str = "warm") -> dict[str, str]:
    return {"Outreach": fx.to_csv([
        fx.PERSON_HEADER,
        ["Pat Example", "https://www.linkedin.com/in/pat-example-123/", company, "", "Message sent", "", "", notes],
    ])}


def _pat_company() -> str | None:
    with session_scope() as db:
        p = db.scalar(select(Prospect).where(Prospect.name == "Pat Example"))
        assert p is not None
        return db.scalar(select(Company.name).where(Company.id == p.company_id)) if p.company_id else None


def test_company_set_in_the_app_stands_until_the_company_cell_changes(sync) -> None:  # type: ignore[no-untyped-def]
    sync(_outreach(), tabs=(OUTREACH,))
    assert _pat_company() == "Pat Co"
    with session_scope() as db:
        pid = db.scalar(select(Prospect.id).where(Prospect.name == "Pat Example"))
        prospects.update(db, pid, company="Pat Holdings")
    sync(_outreach(notes="hot lead"), tabs=(OUTREACH,))  # another cell changed: the row is re-applied
    assert _pat_company() == "Pat Holdings"
    sync(_outreach(company="Pat Group", notes="hot lead"), tabs=(OUTREACH,))  # the company cell changed: the sheet wins
    assert _pat_company() == "Pat Group"

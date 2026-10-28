from __future__ import annotations

from datetime import date

from sales0.services.sheetsync import (
    SheetCompetitor,
    SheetDiscarded,
    SheetEnquiry,
    SheetGroup,
    SheetMetricRow,
    SheetOutreachPerson,
    SheetProspect,
    parse_tab,
)
from tests.unit.sheetsync import fixtures as fx

TAB = {t.name: t for t in fx.TABS}


def parse(name: str, text: str):
    return parse_tab(text, TAB[name], today=fx.TODAY)


def by(rows, **match):
    return [r.record for r in rows if all(getattr(r.record, k) == v for k, v in match.items())]


def test_active_enquiries_quirks() -> None:
    p = parse("Active Enquiries", fx.active_csv())
    assert p.header_row == 1 and p.fatal is None
    recs = {r.row_number: r.record for r in p.rows}
    assert all(isinstance(r, SheetEnquiry) for r in recs.values())

    r2 = recs[2]  # row 2 = first data row
    assert r2.date_added == date(2026, 7, 12) and r2.date_posted == date(2026, 7, 9)
    assert r2.emails == ("a.one@acme.example", "b.two@acme.example")
    assert r2.phones == ("+919876543210",)
    assert r2.key == "post:7000000000000000001" and r2.key_source == "post"
    assert r2.post_url == "linkedin.com/posts/x-activity-7000000000000000001-AbCd"
    assert (r2.summary_company, r2.summary_topic, r2.summary_city, r2.engagement_mode) == ("Acme Corp", "GenAI workshop", "Pune", "online")
    assert r2.priority == "high" and r2.sheet_status == "Email Sent" and r2.sheet_notes == "Called 12/07"
    assert r2.full_enquiry == "Hello 😀 we’re hiring trainers\nsecond line"  # mojibake + CRLF repaired
    assert r2.date_email_sent == date(2026, 7, 13)

    r3 = recs[3]  # bold wrapped, Not provided
    assert r3.requirement_summary == "Beta Ltd - Agents bootcamp - Delhi | Offline"
    assert r3.emails == () and r3.email_note is None and r3.phones == ()
    assert r3.date_posted == date(2026, 3, 3)  # explicit year kept
    assert r3.key == "post:7000000000000000002"

    r4 = recs[4]
    assert r4.date_posted is None and r4.post_url is None and r4.link_text is None
    assert r4.key_source == "email_summary" and r4.phones == ("+919876543211",)
    assert len(r4.full_enquiry or "") == 20000  # very long cell capped
    assert any(w.code == "cell-truncated" and w.row == 4 for w in p.warnings)

    r5 = recs[5]
    assert r5.emails == ("d@delta.example",) and r5.email_note is None
    assert r5.phones == ("+919000011111", "+918000022222")
    assert r5.link_text == "Some post title | Delta"

    r6 = recs[6]  # profile link typed in phone column
    assert r6.phones == () and r6.contact_link == "linkedin.com/in/some-profile-123"
    assert not any(w.row == 6 and w.code == "bad-phone" for w in p.warnings)


def test_active_blank_placeholder_duplicate_and_bad_rows() -> None:
    p = parse("Active Enquiries", fx.active_csv())
    assert p.blank_rows == 2  # fully blank row + status/priority-only placeholder
    # Same activity id twice: both kept, the second re-keyed by row hash with a warning
    recs = {r.row_number: r.record for r in p.rows}
    assert recs[9].key.startswith("row:") and recs[9].key_source == "row_hash"
    assert any(w.code == "duplicate-key-rekeyed" and w.row == 9 for w in p.warnings)
    # 31 February: row kept, date None, warning names the field but not the value
    assert recs[10].date_posted is None
    assert any(w.code == "bad-date:date_posted" and w.row == 10 for w in p.warnings)
    # junk row with only a stray note -> error with row number, no content in the message
    errs = {e.row: e for e in p.errors}
    assert 11 in errs and errs[11].code == "no-identifying-content"
    assert "stray" not in errs[11].message
    assert p.data_rows == len(p.rows) + len(p.errors)


def test_identical_duplicate_rows_are_an_error_not_an_abort() -> None:
    row = fx.active_row(summary="Same - Thing - Pune | Online", email="s@s.example")
    p = parse("Active Enquiries", fx.active_csv([row, row, row]))
    assert len(p.rows) == 2 and [e.code for e in p.errors] == ["duplicate-row"]


def test_year_inference_across_new_year() -> None:
    row = fx.active_row(posted="28 December", added="02 January", summary="A - B - C | Online", email="a@a.example")
    rec = parse("Active Enquiries", fx.active_csv([row])).rows[0].record
    assert rec.date_added == date(2026, 1, 2) and rec.date_posted == date(2025, 12, 28)


def test_header_whitespace_case_and_column_order_do_not_matter() -> None:
    header = [h.upper() + "  " for h in reversed(fx.ACTIVE_HEADER)]
    row = list(reversed(fx.active_row(summary="X - Y - Z | Online", email="x@x.example")))
    p = parse("Active Enquiries", fx.to_csv([header, row]))
    assert p.rows and p.rows[0].record.emails == ("x@x.example",)


def test_missing_header_is_fatal_not_exception() -> None:
    p = parse("Active Enquiries", "just,some\nrandom,rows\n")
    assert p.fatal == "header-not-found" and not p.rows


def test_discarded() -> None:
    p = parse("Discarded Enquiries", fx.discarded_csv())
    assert len(p.rows) == 2 and p.blank_rows == 1
    a, b = (r.record for r in p.rows)
    assert isinstance(a, SheetDiscarded)
    assert a.discard_reason == "they are looking for trainers" and a.sheet_status == "Discarded"
    assert a.emails == ("o@omega.example", "o2@omega.example") and a.date_email_sent is None
    assert b.sheet_status == "No Response" and b.emails == ()
    assert b.phones == () and not any(w.row == 3 for w in p.warnings)


def test_outreach_and_prospects_share_a_key() -> None:
    o = parse("Outreach", fx.outreach_csv())
    pr = parse("Prospects", fx.prospects_csv())
    assert len(o.rows) == 3 and o.blank_rows == 1
    pat_o = by(o.rows, linkedin_slug="pat-example-123")[0]
    pat_p = by(pr.rows, linkedin_slug="pat-example-123")[0]
    assert isinstance(pat_o, SheetOutreachPerson) and isinstance(pat_p, SheetProspect)
    assert pat_o.record_id == pat_p.record_id == ("prospect", "li:pat-example-123")
    assert pat_o.reason_url and pat_o.extra_link  # blank-header spare column is read
    assert pat_o.sheet_status == "Message sent" and pat_o.linkedin_url == "linkedin.com/in/pat-example-123"
    sam = by(o.rows, name="Sam Sample")[0]
    assert sam.key_source == "name_company" and sam.key.startswith("nc:")
    assert not o.warnings  # short links (lnkd.in) are not a warning


def test_working_sheet_blank_and_typo_headers() -> None:
    p = parse("Working Sheet", fx.working_csv())
    assert len(p.rows) == 1
    rec = p.rows[0].record
    assert (rec.name, rec.company, rec.reason_text) == ("Wes Work", "Work Co", "met at event")


def test_competitors() -> None:
    p = parse("Competitors", fx.competitors_csv())
    recs = [r.record for r in p.rows]
    assert all(isinstance(r, SheetCompetitor) for r in recs)
    assert recs[0].key == "web:rival.example" and recs[0].approx_clients == 12 and recs[0].emails == ("info@rival.example",)
    assert recs[1].key == "web:rival.example/about"
    assert recs[2].key == "name:nameonly co" and recs[2].approx_clients is None
    assert any(w.code == "bad-number:approx_clients" for w in p.warnings)


def test_groups() -> None:
    p = parse("Groups", fx.groups_csv())
    recs = [r.record for r in p.rows]
    assert all(isinstance(r, SheetGroup) for r in recs)
    assert recs[0].key.startswith("link:") and recs[0].keep_level == "keep" and "✨" in recs[0].name
    assert recs[1].keep_level == "keep-low"
    assert recs[2].key == "name:no link" and recs[2].keep_level is None
    assert any(w.code == "unknown-group-details" for w in p.warnings)


def test_weekly_duplicate_week_header_ragged_rows_and_numbers() -> None:
    p = parse("Weekly", fx.weekly_csv())
    assert any(w.code == "duplicate-week-header" for w in p.warnings)
    recs = {r.record.metric: r.record for r in p.rows}
    assert isinstance(recs["Number of Enquiries added"], SheetMetricRow)
    w = recs["Number of Enquiries added"].weeks
    assert [(x.label, x.occurrence, x.value) for x in w] == [
        ("Week 35", 1, 3.0), ("Week 36", 1, 4.0), ("Week 43", 1, 2.0), ("Week 43", 2, 5.0), ("Week 44", 1, 1200.0),
    ]
    conv = recs["Conversion"].weeks
    assert [(x.label, x.occurrence, x.raw, x.value) for x in conv] == [
        ("Week 35", 1, "10%", 10.0),
        ("Week 36", 1, "-", None),
        ("Week 43", 1, "n/a", None),
        ("Week 43", 2, "₹5,000", 5000.0),
    ]
    assert recs["Empty data"].weeks == ()
    assert p.blank_rows == 1 and not p.errors


def test_secondary_groups_carry_down_and_duplicate_metric_names() -> None:
    p = parse("Secondary", fx.secondary_csv())
    recs = [r.record for r in p.rows]
    assert [(r.group, r.metric, r.weekly_target) for r in recs] == [
        ("Enquiries added", "LinkedIn Scraping", 15.0),
        ("Enquiries added", "WhatsApp Scraping", 5.0),
        ("Outbound", "Messages Sent", 25.0),
        ("Outbound", "Messages Sent", None),
    ]
    assert len({r.key for r in recs}) == 4  # repeated name stays distinct
    assert [e.code for e in p.errors] == ["metric-name-missing"]  # values without a metric name
    assert p.blank_rows == 1


def test_no_personal_data_in_issue_messages() -> None:
    texts = fx.all_tabs_csv()
    for name, text in texts.items():
        p = parse(name, text)
        for issue in [*p.errors, *p.warnings]:
            blob = f"{issue.code} {issue.message}".lower()
            for secret in ("@", "acme", "linkedin.com", "stray", "pat example", "+91"):
                assert secret not in blob, (name, blob)


def test_person_key_keeps_the_linkedin_kind_and_decodes_the_slug() -> None:
    rows = [
        ["Pat", "https://www.linkedin.com/in/Pat-Example%2D123/", "Pat Co", "", "", "", "", ""],
        ["Acme page", "linkedin.com/company/Acme-Corp", "Acme", "", "", "", "", ""],
        ["Short", "https://lnkd.in/p/abc", "Short Co", "", "", "", "", ""],
    ]
    p = parse("Prospects", fx.to_csv([fx.PERSON_HEADER, *rows]))
    keys = [r.record.key for r in p.rows]
    assert keys[0] == "li:pat-example-123" and keys[1] == "li:company/acme-corp"
    assert keys[2].startswith("nc:")  # unresolved short link: name + company
    assert p.rows[1].record.linkedin_slug == "acme-corp"

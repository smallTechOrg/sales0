"""SYNTHETIC sheet fixtures (no real rows or personal data) reproducing the real quirks.

Quirks covered: trailing-space headers, dates without a year, 'Not provided' / '-',
multiple e-mails, spaced / bracketed phone numbers, a profile link typed into the phone
column, blank and ragged rows, pre-filled placeholder rows, bold-wrapped summaries,
mojibake + emoji, very long cells, the blank header column, a duplicate 'Week 43'
header, a grouped Secondary layout and a duplicated post link.
"""

from __future__ import annotations

import csv
import io
from datetime import date

from sales0.services.sheetsync import TabSpec

TODAY = date(2026, 10, 28)
SID, MID = "sheet-main", "sheet-metrics"

TABS = (
    TabSpec("Active Enquiries", "enquiries", SID, "0"),
    TabSpec("Discarded Enquiries", "discarded", SID, "11"),
    TabSpec("Outreach", "outreach", SID, "12"),
    TabSpec("Prospects", "prospects", SID, "13"),
    TabSpec("Working Sheet", "working", SID, "14"),
    TabSpec("Competitors", "competitors", SID, "15"),
    TabSpec("Groups", "groups", SID, "16"),
    TabSpec("Weekly", "weekly", MID, "21"),
    TabSpec("Secondary", "secondary", MID, "22"),
)

ACTIVE_HEADER = [
    "Date On Which Enquiry Was Posted", "Date Enquiry Added", "Requirement Summary", "E-Mail", "Priority",
    "Status", "Notes", "Company", "Date E-mail Sent", "Date Proposal Sent", "Contact Number ", "Link",
    "Full Enquiry", "Keyword Used", "Update", "Contact person",
]
DISCARDED_HEADER = [*ACTIVE_HEADER[:8], "Date Message Sent", "Date Proposal Sent", "Contact Number ", "Link", "Full Enquiry"]
PERSON_HEADER = ["Name", "Linkedin ID", "Company Name ", "Website", "Status", "Reason of adding ", "", "Notes"]


def to_csv(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def active_row(**kw: str) -> list[str]:
    base = {h: "" for h in ACTIVE_HEADER}
    names = {
        "posted": ACTIVE_HEADER[0], "added": ACTIVE_HEADER[1], "summary": ACTIVE_HEADER[2], "email": ACTIVE_HEADER[3],
        "priority": ACTIVE_HEADER[4], "status": ACTIVE_HEADER[5], "notes": ACTIVE_HEADER[6], "company": ACTIVE_HEADER[7],
        "sent": ACTIVE_HEADER[8], "proposal": ACTIVE_HEADER[9], "phone": ACTIVE_HEADER[10], "link": ACTIVE_HEADER[11],
        "full": ACTIVE_HEADER[12], "keyword": ACTIVE_HEADER[13], "update": ACTIVE_HEADER[14], "person": ACTIVE_HEADER[15],
    }
    for k, val in kw.items():
        base[names[k]] = val
    return [base[h] for h in ACTIVE_HEADER]


LONG = "Very long requirement. " * 4000  # ~92k chars

ACTIVE_ROWS = [
    active_row(posted="09 July", added="12 July", summary="Acme Corp - GenAI workshop - Pune | Online",
               email="a.one@acme.example, b.two@acme.example", priority="High", status="Email Sent", notes="Called 12/07",
               company="Acme Corp", sent="13 July", phone="+91 98765 43210", link="https://www.linkedin.com/posts/x-activity-7000000000000000001-AbCd?utm_source=share",
               full="Hello ðŸ˜€ weâ€™re hiring trainers\r\nsecond line", keyword='"gen ai" training', person="Asha Test"),
    active_row(posted="03 March 2026", added="05 March", summary="**Beta Ltd - Agents bootcamp - Delhi | Offline**",
               email="Not provided", priority="Medium", status="New", company="Beta Ltd", phone="Not provided",
               link="https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000002/"),
    active_row(posted="-", added="", summary="Gamma - RAG consulting - Mumbai | Hybrid", email="g@gamma.example",
               priority="Low", status="Closed", phone="098765 43211", link="-", full=LONG),
    active_row(summary="Delta - LLM talk - Chennai | Online", email="d@delta.example — call after 5", phone="(Mob - Dee) +91 90000 11111 / 80000 22222",
               link="Some post title | Delta"),
    active_row(summary="Epsilon - Chatbot - Goa", email="", phone="https://www.linkedin.com/in/some-profile-123/", status="Follow Up"),
    [""] * 16,  # blank row
    ["", "", "", "", "High", "Discarded"],  # placeholder, ragged
    active_row(summary="Zeta - Duplicate post - Pune | Online", email="z@zeta.example",
               link="https://www.linkedin.com/posts/y-activity-7000000000000000001-ZzZz"),  # same activity id as row 1
    active_row(posted="31 February", summary="Eta - Bad date - Pune | Online", email="e@eta.example"),
    ["", "", "", "", "", "Closed", "stray note"],  # junk row: only status + a note
]


def active_csv(rows: list[list[str]] | None = None) -> str:
    return to_csv([ACTIVE_HEADER, *(rows if rows is not None else ACTIVE_ROWS)])


def discarded_csv() -> str:
    h = DISCARDED_HEADER
    rows = [
        ["10 July", "11 July", "Omega - Intern hiring - Pune", "o@omega.example,o2@omega.example", "", "Discarded", "they are looking for trainers",
         "Omega", "-", "-", "98 7654 3210", "https://www.linkedin.com/posts/q-activity-7000000000000000050-QqQq", "Full text ✨"],
        ["", "", "Sigma - Vendor pitch - Pune", "-", "", "No Response", "no reply after 2 follow-ups", "Sigma", "", "", "-", "-", ""],
        ["", "", "", "", "", "Discarded", "", "", "", "", "", "", ""],
    ]
    return to_csv([h, *rows])


def outreach_csv() -> str:
    return to_csv([
        PERSON_HEADER,
        ["Pat Example", "https://www.linkedin.com/in/pat-example-123/?trk=x", "Pat Co", "https://patco.example/", "Message sent",
         "https://www.linkedin.com/posts/p-activity-7000000000000000099-aaaa", "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000098", "warm"],
        ["Sam Sample", "", "Sam Inc", "", "Connection request sent", "", "", ""],
        ["Lee Link", "https://lnkd.in/p/abc", "Lee LLC", "", "Closed", "", "", "x"],
        ["", "", "", "", "", "", "", ""],
    ])


def prospects_csv() -> str:
    return to_csv([
        PERSON_HEADER,
        ["Pat Example", "https://linkedin.com/in/pat-example-123", "Pat Co", "", "", "https://example.test/post", "", "also prospect"],
        ["New Person", "https://www.linkedin.com/in/new-person-9/", "", "", "", "", "", "n"],
    ])


def working_csv() -> str:
    return to_csv([["", "Name", "Comapany", "Reason"], ["1", "Wes Work", "Work Co", "met at event"], ["2", "", "", ""]])


def competitors_csv() -> str:
    return to_csv([
        ["Website", "Name", "Approx no of Clients", "Some Client Names", "Contact", "E-Mail", "Notes"],
        ["https://www.rival.example/", "Rival One", "12", "", "", "info@rival.example", "n"],
        ["rival.example/about", "Rival Dup", "", "", "", "", ""],
        ["", "Nameonly Co", "many", "", "", "https://rival2.example/", ""],
    ])


def groups_csv() -> str:
    return to_csv([
        ["Name", "Platform", "Link", "Details"],
        ["AI Folks ✨🤓", "WhatsApp", "https://chat.example/abc123", "keep"],
        ["Low group ", "LinkedIn", "https://www.linkedin.com/groups/123/", "keep-low "],
        ["No link", "Telegram", "not a link", ""],
        ["Weird", "FB", "https://fb.example/g", "maybe"],
    ])


def weekly_csv() -> str:
    h = ["Key Metric", "Week 35", "Week 36", "Week 43", "Week 43", "Week 44", "", ""]
    return to_csv([
        h,
        ["Number of Enquiries added", "3", "4", "2", "5", "1,200", "", ""],
        ["Conversion", "10%", "-", "n/a", "₹5,000", ""],  # ragged
        ["", "", "", "", "", "", "", ""],
        ["Empty data", "", "", "", "", "", "", ""],
    ])


def secondary_csv() -> str:
    h = ["Metrics", "", "Target - Weekly", "Week 35", "Week 36"]
    return to_csv([
        h,
        ["Enquiries added", "LinkedIn Scraping", "15", "2", "4"],
        ["", "WhatsApp Scraping", "5", "1", "0"],
        ["", "", "", "", ""],
        ["Outbound", "Messages Sent ", "25", "3", ""],
        ["", "Messages Sent", "", "", "1"],
        ["", "", "", "7", ""],
    ])


def all_tabs_csv() -> dict[str, str]:
    return {
        "Active Enquiries": active_csv(),
        "Discarded Enquiries": discarded_csv(),
        "Outreach": outreach_csv(),
        "Prospects": prospects_csv(),
        "Working Sheet": working_csv(),
        "Competitors": competitors_csv(),
        "Groups": groups_csv(),
        "Weekly": weekly_csv(),
        "Secondary": secondary_csv(),
    }

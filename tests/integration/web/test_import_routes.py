"""Import page (Phase 2 slice S4): paste a single post (one triage call) and CSV import, batch summary,
errors, duplicates, size/emptiness limits and escaping.

The happy-path paste uses the REAL Haiku key from ``.env`` (conftest fails the session without it); the
failure and duplicate paths use the scripted fake LLM.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sales0.db.engine import session_scope
from sales0.db.models import Enquiry, ImportBatch
from sales0.llm import LLMTransientError, set_llm_client
from sales0.web.app import create_app
from tests.integration.web.conftest import HX
from tests.support.fake_llm import FakeLLMClient, triage_answer

JSON = {"Accept": "application/json"}
POST_TEXT = (
    "Hi connections! Our company in Pune is looking for a corporate trainer to run a two day hands-on "
    "Generative AI workshop for about 40 managers next month. Please share your profile and rates. "
    "Contact hr@punelogistics.example"
)
CLIENTS_CSV = "name,company,started_on,notes\nOld Customer Ltd,Old Customer Ltd,2023-04-01,Since 2023\nSecond Client,Second Co,,\n"
COMPETITORS_CSV = "Name,Website,Approx. clients\nFresh Rival,https://fresh-rival.example,12\n"


@pytest.fixture
def web(db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _main(html: str) -> str:
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


def _enquiry_count() -> int:
    with session_scope() as db:
        return int(db.scalar(select(func.count()).select_from(Enquiry)) or 0)


def _csv(web: TestClient, kind: str, content: str, name: str = "data.csv", *, headers: dict | None = None, follow: bool = False):  # noqa: ANN202
    return web.post(
        "/import/csv", data={"kind": kind}, files={"file": (name, content.encode(), "text/csv")},
        headers=headers or {}, follow_redirects=follow,
    )  # fmt: skip


def test_import_page_has_both_cards_the_sheet_note_and_empty_batches(web: TestClient) -> None:
    html = web.get("/import").text
    main = _main(html)
    for hook in ('id="paste-form"', 'id="csv-form"', 'id="batches-empty"', 'id="paste-text"', 'id="csv-file"'):
        assert hook in main, hook
    assert "Your Google Sheets are synced automatically" in main and 'href="/sync"' in main
    assert "Nothing imported yet." in main
    for label in ("Enquiries", "Discarded enquiries", "Outreach", "Prospects", "Working sheet", "Competitors", "Groups", "Legacy clients", "Contacts"):
        assert f">{label}</option>" in main, label
    assert 'enctype="multipart/form-data"' in main and 'minlength="40"' in main
    assert "Coming in Phase 2" not in html


@pytest.mark.llm
def test_paste_a_post_lands_in_review_with_a_verdict_real_haiku(web: TestClient) -> None:
    before = _enquiry_count()
    resp = web.post("/import/paste", data={"text": POST_TEXT, "author": "Pune HR", "link": ""}, follow_redirects=False)
    assert resp.status_code == 303 and re.fullmatch(r"/import\?pasted=\d+&outcome=created", resp.headers["location"])
    assert _enquiry_count() == before + 1
    page = web.get(resp.headers["location"]).text
    assert 'id="paste-result"' in page and re.search(r"Added - AI says (Genuine|Discard)", page)
    assert 'id="result-link"' in page
    with session_scope() as db:
        row = db.scalars(select(Enquiry).order_by(Enquiry.id.desc())).first()
        assert row is not None and row.ai_verdict in ("genuine", "discard") and row.triage_status == "done"
        assert row.review_status == "pending_review"
    # the same text again: duplicate, nothing new, and a clear message with a link
    again = web.post("/import/paste", data={"text": POST_TEXT}, follow_redirects=False)
    assert again.status_code == 303 and "outcome=duplicate" in again.headers["location"]
    assert _enquiry_count() == before + 1
    dup = web.get(again.headers["location"]).text
    assert "Already in your inbox" in dup and 'id="result-link"' in dup
    # htmx path redirects to the same result panel
    hx = web.post("/import/paste", data={"text": POST_TEXT}, headers=HX)
    assert hx.status_code == 204 and "outcome=duplicate" in hx.headers["hx-redirect"]


def test_paste_validation_and_triage_failure_paths(web: TestClient) -> None:
    set_llm_client(FakeLLMClient([triage_answer(verdict="genuine")]))
    short = web.post("/import/paste", data={"text": "too short"}, headers=HX)
    assert "toast--error" in short.text
    plain = web.post("/import/paste", data={"text": "too short"}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == "/import"
    assert "flash--error" in web.get("/import").text
    api = web.post("/import/paste", data={"text": "x"}, headers=JSON)
    assert api.status_code == 400 and api.json()["error"]["code"] == "paste_invalid"
    assert _enquiry_count() == 0

    set_llm_client(FakeLLMClient([LLMTransientError("overloaded")] * 3))
    failed = web.post("/import/paste", data={"text": POST_TEXT + " (second variant)"}, follow_redirects=False)
    # Either the enquiry is kept with triage_status=failed (service contract) or the route reports the AI outage.
    if failed.status_code == 303 and "pasted=" in failed.headers["location"]:
        assert "Added - AI triage failed" in web.get(failed.headers["location"]).text
        assert _enquiry_count() == 1
    else:
        assert "flash--error" in web.get("/import").text


def test_csv_import_clients_summary_and_idempotent_reimport(web: TestClient) -> None:
    resp = _csv(web, "clients", CLIENTS_CSV, "clients.csv")
    assert resp.status_code == 303 and re.fullmatch(r"/import/batches/\d+", resp.headers["location"])
    page = web.get(resp.headers["location"]).text
    assert 'id="import-summary"' in page and "clients.csv" in page
    assert re.search(r'id="sum-total">2<', page) and re.search(r'id="sum-new">2<', page) and re.search(r'id="sum-errors">0<', page)
    assert "Imported clients.csv: 2 new" in page
    clients_tab = web.get("/pipeline?stage=client").text
    assert "Old Customer Ltd" in clients_tab and "Second Co" in clients_tab  # visible under Pipeline -> Clients (as companies)

    again = _csv(web, "clients", CLIENTS_CSV, "clients.csv")
    page2 = web.get(again.headers["location"]).text
    assert re.search(r'id="sum-new">0<', page2) and re.search(r'id="sum-unchanged">2<', page2)
    assert "this file was already imported" in page2
    listing = web.get("/import").text
    assert 'id="batch-list"' in listing and listing.count("clients.csv") == 2
    assert len(re.findall(r'<tr id="batch-\d+"', listing)) == 2


def test_csv_import_competitors_and_row_problems_are_reported_without_cell_content(web: TestClient) -> None:
    ok = _csv(web, "competitors", COMPETITORS_CSV, "rivals.csv")
    assert ok.status_code == 303
    assert "Fresh Rival" in web.get("/competitors").text
    # a row with no identifying content is an error; the rest import
    mixed = "Name,Website,Approx. clients\nGood Rival,https://good-rival.example,3\n,,\n"
    resp = _csv(web, "competitors", mixed, "mixed.csv")
    page = web.get(resp.headers["location"]).text
    assert 'id="import-errors"' in page
    assert "Good Rival" not in page  # summary pages carry counts, rows and codes only
    with session_scope() as db:
        batch = db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc())).first()
        assert batch is not None and batch.rows_new >= 1
    assert "Good Rival" in web.get("/competitors").text


def test_csv_upload_error_paths(web: TestClient) -> None:
    empty = _csv(web, "clients", "", "empty.csv", headers=HX)
    assert "Choose a CSV file and what it contains." in empty.text and "toast--error" in empty.text
    bad_kind = _csv(web, "bogus", CLIENTS_CSV, headers=HX)
    assert "Choose a CSV file and what it contains." in bad_kind.text
    paste_kind = _csv(web, "paste", CLIENTS_CSV, headers=HX)
    assert "toast--error" in paste_kind.text
    big = _csv(web, "clients", "name\n" + ("x" * 100 + "\n") * 60000, "big.csv", headers=HX)
    assert "over 5 MB" in big.text
    plain = _csv(web, "bogus", CLIENTS_CSV)
    assert plain.status_code == 303 and plain.headers["location"] == "/import"
    assert "Choose a CSV file and what it contains." in web.get("/import").text
    missing_file = web.post("/import/csv", data={"kind": "clients"})
    assert missing_file.status_code in (400, 422)
    assert web.get("/import/batches/99999").status_code == 404
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(ImportBatch)) == 0


def test_json_csv_import_and_batch(web: TestClient) -> None:
    resp = web.post("/import/csv", data={"kind": "clients"}, files={"file": ("c.csv", CLIENTS_CSV.encode(), "text/csv")}, headers=JSON)
    assert resp.status_code == 201 and resp.json()["data"]["new"] == 2
    batch = web.get(f"/import/batches/{resp.json()['data']['id']}", headers=JSON).json()["data"]
    assert batch["total"] == 2 and batch["kind"] == "clients"


def test_filename_is_escaped(web: TestClient) -> None:
    name = "<img src=x onerror=alert(1)>.csv"
    resp = _csv(web, "clients", CLIENTS_CSV, name)
    page = web.get(resp.headers["location"]).text
    listing = web.get("/import").text
    for html in (page, listing):
        assert "<img src=x onerror" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html

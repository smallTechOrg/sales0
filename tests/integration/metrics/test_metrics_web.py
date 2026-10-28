"""The /metrics page, CSV exports, manual figure and target forms, and the digest section."""

from __future__ import annotations

import csv
import io
import re

import pytest
from fastapi.testclient import TestClient

from sales0.config import reset_settings
from sales0.db.engine import session_scope
from sales0.db.models import Keyword, ScrapeRun, ScrapeRunKeyword
from sales0.services import digest
from sales0.services.metrics import digest_section, report
from sales0.web.app import create_app
from tests.support import metrics_seed as S

@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "utcnow", lambda: S.NOW)


@pytest.fixture
def client(db_url: str):  # type: ignore[no-untyped-def]
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def seeded_client(client: TestClient):  # type: ignore[no-untyped-def]
    with session_scope() as db:
        S.seed(db)
        S.seed_sheet_baseline(db)
    return client


def _rows(html: str, table_id: str) -> list[str]:
    table = re.search(rf'<table[^>]*id="{table_id}".*?</table>', html, re.S)
    assert table, table_id
    return re.findall(r"<tr.*?</tr>", table.group(0), re.S)


def test_empty_database_shows_empty_state_and_every_section(client: TestClient) -> None:
    html = client.get("/metrics").text
    assert 'id="metrics-empty"' in html and "No data yet" in html
    for sid in ("weekly-table", "secondary-table", "funnel-table", "keyword-table", "group-table", "cost-table"):
        assert f'id="{sid}"' in html
    assert "No keyword runs yet." in html
    assert "not enabled" in html  # WhatsApp row
    assert "<svg" in html and "<script" not in html.split("<main", 1)[1]  # inline charts, no JS on the page


def test_page_with_data_has_exact_cells_charts_and_source_labels(seeded_client: TestClient) -> None:
    html = seeded_client.get("/metrics?weeks=12&mode=max").text
    assert 'id="metrics-empty"' not in html
    assert re.search(r'data-week="2026-W38" data-source="computed">40 ', html)
    assert re.search(r'data-week="2026-W39" data-source="sheet">999 ', html)
    assert re.search(r'data-week="2026-W36" data-source="sheet">7 ', html)
    assert re.search(r'data-week="2026-W35" data-source="none">- <', html) or 'data-week="2026-W35" data-source="none">-' in html
    assert html.count("<svg") >= 4 and "<title" in html and "<desc" in html
    assert "prune candidate" in html  # kw_dead
    sheet_html = seeded_client.get("/metrics?weeks=12&mode=sheet").text
    assert re.search(r'data-week="2026-W38" data-source="sheet">5 ', sheet_html)
    assert "Last 12" in html and 'value="12" selected' in html


def test_bad_query_values_fall_back_safely(seeded_client: TestClient) -> None:
    for q in ("weeks=abc", "weeks=9999", "mode=zzz", "week=nonsense", "week=2099-W01", "sort=zzz&dir=zzz"):
        assert seeded_client.get(f"/metrics?{q}").status_code == 200, q


def test_keyword_table_sorts_both_ways(seeded_client: TestClient) -> None:
    def order(q: str) -> list[str]:
        rows = _rows(seeded_client.get(f"/metrics?{q}").text, "keyword-table")
        return [m.group(1) for r in rows if (m := re.search(r'data-keyword="([^"]+)"', r))]

    assert order("sort=text&dir=asc") == ["kw_alpha", "kw_beta", "kw_dead"]
    assert order("sort=text&dir=desc") == ["kw_dead", "kw_beta", "kw_alpha"]
    assert order("sort=scanned&dir=desc") == ["kw_alpha", "kw_beta", "kw_dead"]
    assert order("sort=scanned&dir=asc")[0] == "kw_dead"
    assert order("sort=relevant&dir=desc")[0] == "kw_alpha"


def test_keyword_text_is_escaped(client: TestClient) -> None:
    with session_scope() as db:
        k = Keyword(text="<script>alert(1)</script>", text_key="xss")
        db.add(k)
        db.flush()
        run = ScrapeRun(trigger="manual", mode="live", status="succeeded", started_at=S.NOW)
        db.add(run)
        db.flush()
        db.add(ScrapeRunKeyword(run_id=run.id, keyword_id=k.id, keyword_text=k.text, status="done", posts_seen=50))
    html = client.get("/metrics").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    csv_text = client.get("/metrics/export/keywords.csv").text
    assert "<script>" in csv_text  # CSV is data, not HTML


def test_csv_exports_match_the_tables(seeded_client: TestClient) -> None:
    for name in ("weekly", "secondary", "funnel", "keywords", "groups", "cost"):
        r = seeded_client.get(f"/metrics/export/{name}.csv?weeks=7&mode=computed")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        assert f"sales0-metrics-{name}.csv" in r.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(r.text)))
        assert len(rows) >= (1 if name == "groups" else 2), name
    weekly = list(csv.reader(io.StringIO(seeded_client.get("/metrics/export/weekly.csv?weeks=7&mode=computed").text)))
    assert weekly[0][1:] == S.WEEKS
    assert weekly[1][0] == "Number of Enquiries added" and weekly[1][1:] == [str(x) for x in S.EXPECTED["enquiries_added"]]
    assert weekly[2][1:] == ["computed"] * 7
    funnel = list(csv.reader(io.StringIO(seeded_client.get("/metrics/export/funnel.csv?weeks=7").text)))
    assert funnel[1][:2] == ["Scanned", "2090"]
    assert seeded_client.get("/metrics/export/nope.csv").status_code == 404


def test_csv_neutralises_formula_cells(client: TestClient) -> None:
    with session_scope() as db:
        k = Keyword(text="=HYPERLINK(\"http://x\")", text_key="f1")
        db.add(k)
        db.flush()
        run = ScrapeRun(trigger="manual", mode="live", status="succeeded", started_at=S.NOW)
        db.add(run)
        db.flush()
        db.add(ScrapeRunKeyword(run_id=run.id, keyword_id=k.id, keyword_text=k.text, status="done", posts_seen=1))
    rows = list(csv.reader(io.StringIO(client.get("/metrics/export/keywords.csv").text)))
    assert rows[1][0].startswith("'=")


def test_manual_form_saves_and_shows_manual_label(seeded_client: TestClient) -> None:
    r = seeded_client.post("/metrics/manual", data={"metric": "posts_published", "week": "2026-W44", "value": "6", "back": "/metrics?weeks=7&mode=max"},
                           follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/metrics")
    html = seeded_client.get("/metrics?weeks=7").text
    assert re.search(r'data-week="2026-W44" data-source="manual">6 ', html)
    bad = seeded_client.post("/metrics/manual", data={"metric": "posts_published", "week": "2026-W99", "value": "1"}, follow_redirects=True)
    assert "not a valid ISO week" in bad.text
    bad2 = seeded_client.post("/metrics/manual", data={"metric": "posts_published", "week": "2026-W44", "value": "abc"}, follow_redirects=True)
    assert "whole number" in bad2.text
    seeded_client.post("/metrics/manual", data={"metric": "posts_published", "week": "2026-W44", "value": ""})
    assert not re.search(r'data-week="2026-W44" data-source="manual"', seeded_client.get("/metrics?weeks=7").text)


def test_back_parameter_cannot_redirect_off_site(seeded_client: TestClient) -> None:
    r = seeded_client.post("/metrics/targets", data={"source": "messages_sent", "value": "7", "back": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/metrics"


def test_target_override_roundtrip(seeded_client: TestClient) -> None:
    seeded_client.post("/metrics/targets", data={"source": "first_follow_up", "value": "12"})
    html = seeded_client.get("/metrics").text
    row = next(r for r in _rows(html, "secondary-table") if 'data-source="first_follow_up"' in r)
    assert "12" in row and "(override)" in row
    seeded_client.post("/metrics/targets", data={"source": "first_follow_up", "value": ""})
    row = next(r for r in _rows(seeded_client.get("/metrics").text, "secondary-table") if 'data-source="first_follow_up"' in r)
    assert "(default)" in row
    err = seeded_client.post("/metrics/targets", data={"source": "whatsapp", "value": "3"}, follow_redirects=True)
    assert "no target" in err.text


def test_post_requires_same_origin(seeded_client: TestClient) -> None:
    r = seeded_client.post("/metrics/targets", data={"source": "messages_sent", "value": "7"}, headers={"Origin": "http://evil.example"})
    assert r.status_code in (400, 403)


def test_weekly_pulse_digest_section(seeded_client: TestClient) -> None:
    digest_section.register()
    try:
        with session_scope() as db:
            view = digest.generate_digest(db, trigger="manual", now=S.NOW)
        sec = next(s for s in view.sections if s.key == "weekly_pulse")
        assert sec.error is None and sec.title == "Weekly pulse"
        texts = [digest.item_text(i) for i in sec.items]
        assert "LinkedIn keyword scraping: 38 of 15 (253.3%)" in texts, texts
        assert sec.stats["week"] == "2026-W44"
        assert not any("WhatsApp" in t for t in texts)
    finally:
        digest.unregister_section("weekly_pulse")
        reset_settings()


def test_resolve_pure_rules() -> None:
    r = report.resolve
    assert r("max", "computed", 5, 9).source == "sheet" and r("max", "computed", 9, 9).source == "computed"
    assert r("max", "computed", 0, None).value == 0 and r("max", "computed", None, None).source == "none"
    assert r("computed", "computed", 1, 99).value == 1 and r("sheet", "computed", 1, 99).value == 99
    assert r("sheet", "computed", 1, None).value == 1
    assert r("max", "manual", None, 4, 7).source == "manual" and r("sheet", "manual", None, 4, 7).source == "sheet"
    assert r("max", "manual", None, None, None).shown == "-"

"""Phase 1.1 layout-drift visibility on /runs: a finished run with ``layout_warnings > 0``
shows ``#run-drift-alert`` (warn banner, role=status, link to the per-keyword log) in the
run panel and the run detail, and a "Layout warning" badge in the history. A run without
warnings, and a run still in progress, show none of it. Real app + real SQLite file."""

from __future__ import annotations

import re
from html import unescape

from fastapi.testclient import TestClient

from tests.integration.web.conftest import HX, make_run

DRIFT = "LinkedIn's page layout may have changed — {n} keyword(s) returned results sales0 couldn't fully read. " \
        "Some posts may be missing. See the per-keyword log."


def _panel(html: str) -> str:
    start = html.index('<section id="run-panel"')
    return html[start : html.index("</section>", start)]


def _alert(html: str) -> str:
    m = re.search(r'<div id="run-drift-alert"[^>]*>.*?</div>', html, re.S)
    assert m, "no #run-drift-alert"
    return m.group(0)


def _drift_run(n: int = 1) -> int:
    keywords = [(f"layout-drift-test-{i}", "warning") for i in range(n)] + [("ai workshop", "done")]
    return make_run("succeeded", keywords=keywords, keywords_planned=n + 1, keywords_done=n + 1, posts_seen=4, posts_new=4, posts_relevant=4)


def test_finished_run_with_a_layout_warning_shows_the_drift_alert_everywhere(client: TestClient) -> None:
    run_id = _drift_run(1)
    page = unescape(client.get("/runs").text)
    alert = _alert(_panel(page))
    assert 'class="banner banner--warn"' in alert and 'role="status"' in alert
    assert DRIFT.format(n=1) in alert
    assert f'href="/runs/{run_id}#run-keywords"' in alert
    assert "dismiss" not in alert.lower()
    # Runs stay allowed: the alert never disables Run now by itself.
    assert "Last run finished — 4 new enquiries" in _panel(page)

    row = page[page.index(f'<tr id="run-row-{run_id}">') :].split("</tr>", 1)[0]
    assert 'class="badge badge--warn"' in row and "Layout warning" in row

    detail = unescape(client.get(f"/runs/{run_id}").text)
    assert DRIFT.format(n=1) in _alert(detail)
    assert detail.index('id="run-drift-alert"') < detail.index('id="run-keywords"')
    table = detail.split('id="run-keywords"', 1)[1].split("</table>", 1)[0]
    assert re.search(r"layout-drift-test-0</td>\s*<td>\s*<span class=\"badge badge--warn\">Warning</span>", table), table

    progress = unescape(client.get(f"/runs/{run_id}/progress", headers=HX).text)
    assert DRIFT.format(n=1) in _alert(progress)


def test_alert_counts_every_warning_keyword(client: TestClient) -> None:
    _drift_run(2)
    assert DRIFT.format(n=2) in _alert(unescape(client.get("/runs").text))


def test_runs_without_warnings_or_still_running_show_no_drift_alert(client: TestClient) -> None:
    clean = make_run("succeeded", keywords=[("ai workshop", "done")], posts_new=1)
    page = client.get("/runs").text
    assert 'id="run-drift-alert"' not in page and "Layout warning" not in page
    assert 'id="run-drift-alert"' not in client.get(f"/runs/{clean}").text

    # A warning row on a run that is still active: the alert waits for the run to finish.
    active = make_run("running", keywords=[("layout-drift-test", "warning"), ("ai consulting", "running")],
                      keywords_planned=2, keywords_done=1)
    assert 'id="run-drift-alert"' not in _panel(client.get("/runs").text)
    assert 'id="run-drift-alert"' not in client.get(f"/runs/{active}/progress", headers=HX).text

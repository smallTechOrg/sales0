"""Content-Security-Policy end to end: the real server (fixture LinkedIn, temp DB, port >= 8800) is walked in headless
Chromium and NO CSP violation / page error may occur, while htmx (hx-post form, confirm dialog) and the former
inline handlers (auto-submitting selects) still work. The policy forbids inline script and inline handlers."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Page, expect

from tests.e2e._flows import add_keyword, remove_keyword
from tests.e2e.conftest import _free_port
from tests.support.live_server import live_server

PAGES = [
    "/inbox", "/setup", "/runs", "/pipeline", "/contacts", "/replies", "/metrics", "/costs", "/schedule", "/digest",
    "/suggestions", "/team", "/users", "/outreach/outbox", "/outreach/templates", "/outreach/decks", "/import", "/account",
]

RECORD_VIOLATIONS = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
"""


def test_pages_and_htmx_work_under_the_csp_with_zero_violations(tmp_path: Path, ui: Page) -> None:
    problems: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        text = msg.text
        if msg.type == "error" and ("Content Security Policy" in text or "Refused to" in text):
            problems.append(f"console: {text}")

    ui.on("console", on_console)
    ui.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
    ui.add_init_script(RECORD_VIOLATIONS)

    with live_server(tmp_path / "csp", port=_free_port(8800)) as server:
        base = server.base_url

        for path in PAGES:
            response = ui.goto(f"{base}{path}")
            assert response is not None and response.status < 500, path
            csp = response.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
            assert response.headers["x-frame-options"] == "DENY"
            ui.wait_for_load_state("networkidle")
            problems += [f"{path}: {v}" for v in ui.evaluate("window.__csp")]

        # htmx hx-post form + hx-confirm dialog (the app's own JS) still work.
        ui.goto(f"{base}/setup")
        add_keyword(ui, "csp probe phrase")
        remove_keyword(ui, "csp probe phrase")

        # Former inline `onchange` handler: the select auto-submits the filter form.
        ui.goto(f"{base}/metrics")
        ui.select_option("#weeks-select", index=1)
        ui.wait_for_load_state("networkidle")
        expect(ui).to_have_url(re.compile(r"weeks="))

        ui.wait_for_load_state("networkidle")
        assert problems == [], problems  # the real app caused no violation

        # An injected inline script / handler is blocked (the policy really is enforced).
        ui.goto(f"{base}/inbox")
        ui.evaluate(
            "const s = document.createElement('script'); s.textContent = 'window.__injected = 1'; document.body.appendChild(s);"
        )
        assert ui.evaluate("window.__injected === undefined")
        assert any("script-src" in v for v in ui.evaluate("window.__csp"))

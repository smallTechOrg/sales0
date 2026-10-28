"""Static guard: email can only leave through services/outreach/messages.py, never automatically."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "sales0"
GMAIL_PKG = SRC / "integrations" / "gmail"
MESSAGES = SRC / "services" / "outreach" / "messages.py"
AUTOMATIC_DIRS = (SRC / "jobs", SRC / "agent", SRC / "scraper")
SENDERS = ("send", "retry_send")


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _is_send_draft_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "send_draft"


def _mentions_approved_message(fn: ast.FunctionDef) -> bool:
    for arg in [*fn.args.args, *fn.args.kwonlyargs]:
        if arg.annotation is not None and "ApprovedMessage" in ast.unparse(arg.annotation):
            return True
    return any(isinstance(n, ast.Name) and n.id == "ApprovedMessage" for n in ast.walk(fn))


def test_send_draft_is_only_called_from_the_outreach_state_machine() -> None:
    offenders: list[str] = []
    seen_in_messages = 0
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if _is_send_draft_call(n)]
        if not calls:
            continue
        if GMAIL_PKG in path.parents:
            continue  # the adapter itself (and its live-check CLI helper)
        if path != MESSAGES:
            offenders.extend(f"{path.relative_to(SRC)}:{c.lineno}" for c in calls)  # type: ignore[attr-defined]
            continue
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            inner = [n for n in ast.walk(fn) if _is_send_draft_call(n)]
            if inner and not _mentions_approved_message(fn):
                offenders.append(f"{path.relative_to(SRC)}:{fn.name} calls send_draft without an ApprovedMessage")
            seen_in_messages += len(inner)
    assert not offenders, offenders
    assert seen_in_messages >= 1  # the guard is not vacuous


def _imports_sender(tree: ast.AST) -> list[str]:
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {a.name for a in node.names}
            if module.endswith("outreach.messages") and names & set(SENDERS):
                bad.append(f"from {module} import {', '.join(sorted(names & set(SENDERS)))}")
            if module.endswith("services.outreach") and "messages" in names:
                bad.append(f"from {module} import messages")
        elif isinstance(node, ast.Import):
            bad.extend(f"import {a.name}" for a in node.names if a.name.endswith("outreach.messages"))
    return bad


def test_no_scheduler_job_or_agent_module_can_send() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _modules():
        if not any(d in path.parents for d in AUTOMATIC_DIRS):
            continue
        bad = _imports_sender(ast.parse(path.read_text(encoding="utf-8")))
        if bad:
            offenders[str(path.relative_to(SRC))] = bad
    assert not offenders, offenders


def test_only_web_routes_and_cli_import_the_sender() -> None:
    allowed_dirs = (SRC / "web", SRC / "services" / "outreach")
    offenders = []
    for path in _modules():
        if path == MESSAGES or any(d in path.parents for d in allowed_dirs):
            continue
        if _imports_sender(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders

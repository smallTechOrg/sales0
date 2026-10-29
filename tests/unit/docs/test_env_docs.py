"""The README env table is generated from Settings; it cannot drift."""

from __future__ import annotations

from pathlib import Path

import pytest

from sales0 import envdocs
from sales0.cli import main
from sales0.config import REPO_ROOT, Settings

README = REPO_ROOT / "README.md"


def test_every_setting_has_help_text() -> None:
    assert envdocs.missing_help() == [], "add the new setting(s) to sales0/envdocs.py HELP"


def test_help_has_no_stale_entries() -> None:
    assert set(envdocs.HELP) <= set(Settings.model_fields)


def test_table_lists_every_env_var_and_leaks_no_secret_default() -> None:
    table = envdocs.render_table()
    for name in Settings.model_fields:
        assert f"`{envdocs.env_name(name)}`" in table
    secret_row = next(r for r in table.splitlines() if "SALES0_SECRET_KEY" in r)
    assert "(secret)" in secret_row


def test_readme_block_is_current() -> None:
    assert envdocs.is_current(README), "run: uv run python -m sales0.cli env-docs --write README.md"


def test_cli_check_and_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "R.md"
    f.write_text(f"intro\n{envdocs.START}\nold\n{envdocs.END}\noutro\n")
    assert main(["env-docs", "--check", str(f)]) == 1
    assert main(["env-docs", "--write", str(f)]) == 0
    assert main(["env-docs", "--check", str(f)]) == 0
    text = f.read_text()
    assert text.startswith("intro\n") and text.endswith("outro\n") and "\nold\n" not in text
    assert main(["env-docs", "--write", str(f)]) == 0  # idempotent
    assert f.read_text() == text
    capsys.readouterr()
    assert main(["env-docs"]) == 0
    assert "| Variable | Type |" in capsys.readouterr().out


def test_cli_reports_missing_markers(tmp_path: Path) -> None:
    f = tmp_path / "R.md"
    f.write_text("no markers\n")
    assert main(["env-docs", "--check", str(f)]) == 1

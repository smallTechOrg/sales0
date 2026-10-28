from __future__ import annotations

import json
import stat

import pytest

from sales0.cli import main
from sales0.config import Settings
from sales0.integrations.gmail import live
from sales0.integrations.gmail.auth import (
    SCOPES,
    StoredToken,
    load_token,
    read_client_secret,
    save_token,
)
from sales0.integrations.gmail.client import FakeGmailClient
from sales0.integrations.gmail.errors import GmailNotConfigured


def test_missing_client_secret_message(tmp_path):
    with pytest.raises(GmailNotConfigured, match=str(tmp_path / "x.json")):
        read_client_secret(tmp_path / "x.json")


def test_bad_client_secret(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{}")
    with pytest.raises(GmailNotConfigured, match="not a Google OAuth client JSON"):
        read_client_secret(p)
    p.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}))
    assert read_client_secret(p)["client_id"] == "a"


def test_token_roundtrip_chmod_600(tmp_path):
    p = tmp_path / "sub" / "t.json"
    save_token(p, StoredToken("c", "s", "r", list(SCOPES), "me@example.com"))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert load_token(p).email == "me@example.com"


def test_missing_token_message(tmp_path):
    with pytest.raises(GmailNotConfigured, match="google-auth"):
        load_token(tmp_path / "none.json")


def test_scopes_constant():
    assert "https://www.googleapis.com/auth/gmail.compose" in SCOPES and "openid" in SCOPES
    assert not any("readonly" in s for s in SCOPES)


def test_google_auth_missing_secret_exit1(tmp_path, capsys):
    s = Settings(google_client_secret_path=str(tmp_path / "no.json"), google_token_path=str(tmp_path / "t.json"))
    assert live.google_auth(s) == 1
    assert "no.json" in capsys.readouterr().err


def test_google_auth_ok_with_injected_consent(tmp_path, capsys):
    s = Settings(google_client_secret_path=str(tmp_path / "c.json"), google_token_path=str(tmp_path / "t.json"))
    tok = StoredToken("c", "s", "r", list(SCOPES), "me@example.com")
    assert live.google_auth(s, consent=lambda a, b: tok) == 0
    assert "me@example.com" in capsys.readouterr().out


def test_gmail_check_ok_and_never_sends(capsys):
    g = FakeGmailClient("me@example.com")
    assert live.gmail_check(g) == 0
    assert g.sent == [] and g.raw_drafts == {}
    names = [c[0] for c in g.calls]
    assert "create_draft" in names and "delete_draft" in names and "send_draft" not in names
    assert g.calls[names.index("create_draft")][1][0] == ("me@example.com",)
    assert "OK" in capsys.readouterr().out


def test_send_test_requires_flag():
    g = FakeGmailClient()
    assert live.gmail_send_test(g, yes_send=False) == 2 and g.sent == []


def test_send_test_only_to_self(capsys):
    g = FakeGmailClient("me@example.com")
    assert live.gmail_send_test(g, yes_send=True) == 0
    assert len(g.sent) == 1 and b"To: me@example.com" in g.sent[0]["raw"]
    assert b"TEST" in g.sent[0]["raw"]


def test_send_test_refuses_external(capsys):
    g = FakeGmailClient("me@example.com")
    assert live.gmail_send_test(g, yes_send=True, to="other@example.com") == 2
    assert g.sent == [] and g.raw_drafts == {}


def test_send_test_allow_external_is_explicit():
    g = FakeGmailClient("me@example.com")
    assert live.gmail_send_test(g, yes_send=True, to="other@example.com", allow_external=True) == 0


def test_cli_wiring(monkeypatch, capsys):
    fake = FakeGmailClient("me@example.com")
    monkeypatch.setattr(live, "build_real_client", lambda s: fake)
    assert main(["gmail-check"]) == 0
    assert main(["gmail-send-test"]) == 2  # no --yes-send
    assert main(["gmail-send-test", "--yes-send", "--to", "x@example.com"]) == 2  # --to needs --allow-external
    assert fake.sent == []
    assert main(["gmail-send-test", "--yes-send"]) == 0 and len(fake.sent) == 1


def test_help_hides_to_flag(capsys):
    with pytest.raises(SystemExit):
        main(["gmail-send-test", "-h"])
    out = capsys.readouterr().out
    assert "--to" not in out and "dangerous" in out

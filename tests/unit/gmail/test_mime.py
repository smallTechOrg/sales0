from __future__ import annotations

import pytest

from sales0.integrations.gmail.errors import InvalidMessage
from sales0.integrations.gmail.mime import (
    MAX_MESSAGE_BYTES,
    Attachment,
    build_message,
    normalise,
    parse_raw,
)


def mk(**kw):
    base = dict(to=["a@example.com"], cc=[], subject="Hi", text_body="hello", html_body=None, attachments=[])
    base.update(kw)
    return normalise(**base)


def test_roundtrip_utf8_html_pdf():
    pdf = b"%PDF-1.4\n" + bytes(range(256)) * 10
    c = mk(
        to=["a@example.com", "b@example.org"],
        cc=["c@example.com"],
        subject="Café — नमस्ते ✓",
        text_body="Grüße\r\nline2",
        html_body="<p>Grüße</p>",
        attachments=[Attachment("Proposal é.pdf", pdf, "application/pdf")],
    )
    raw = build_message(c)
    assert b"multipart/mixed" in raw and b"multipart/alternative" in raw
    back = parse_raw(raw)
    assert back == c
    assert back.sha256 == c.sha256
    assert back.subject == "Café — नमस्ते ✓"
    assert back.attachments[0].data == pdf


def test_plain_only_golden():
    raw = build_message(mk()).decode()
    assert "To: a@example.com\r\n" in raw
    assert "Subject: Hi\r\n" in raw
    assert "Content-Type: text/plain" in raw
    assert "multipart" not in raw


def test_hash_changes_with_any_field():
    base = mk().sha256
    assert mk(text_body="hello!").sha256 != base
    assert mk(subject="Hi2").sha256 != base
    assert mk(to=["z@example.com"]).sha256 != base
    assert mk(cc=["z@example.com"]).sha256 != base
    assert mk(html_body="<p>x</p>").sha256 != base
    assert mk(attachments=[Attachment("a.pdf", b"1", "application/pdf")]).sha256 != base


@pytest.mark.parametrize(
    "bad",
    ["a@example.com\r\nBcc: evil@x.com", "a@example.com\nBcc: x@y.com", "no-at-sign", "a@b", "a b@example.com",
     "<a@example.com>", "Name <a@example.com>", "a@@example.com", "", "a..b@example.com", "a@example.com\x00"],
)
def test_bad_addresses_rejected(bad):
    with pytest.raises(InvalidMessage):
        mk(to=[bad])
    with pytest.raises(InvalidMessage):
        mk(cc=[bad])


@pytest.mark.parametrize("subject", ["Hi\r\nBcc: evil@x.com", "Hi\nX: y", "Hi\rX"])
def test_subject_injection_rejected(subject):
    with pytest.raises(InvalidMessage):
        mk(subject=subject)


def test_filename_injection_and_bad_mime_rejected():
    with pytest.raises(InvalidMessage):
        mk(attachments=[Attachment("a.pdf\r\nX: y", b"1", "application/pdf")])
    with pytest.raises(InvalidMessage):
        mk(attachments=[Attachment("../a.pdf", b"1", "application/pdf")])
    with pytest.raises(InvalidMessage):
        mk(attachments=[Attachment("a.pdf", b"1", "application/pdf\r\nX: y")])


def test_no_recipient_rejected():
    with pytest.raises(InvalidMessage):
        mk(to=[])


def test_oversize_clear_error():
    big = Attachment("big.bin", b"\0" * (MAX_MESSAGE_BYTES + 1), "application/octet-stream")
    with pytest.raises(InvalidMessage, match="25 MB"):
        build_message(mk(attachments=[big]))


def test_just_under_limit_ok():
    att = Attachment("big.pdf", b"\x01" * (10 * 1024 * 1024), "application/pdf")
    assert len(build_message(mk(attachments=[att]))) < MAX_MESSAGE_BYTES

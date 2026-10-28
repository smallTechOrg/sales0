"""Local RFC 2822 / MIME building and parsing, plus the canonical approval hash.

Nothing here touches the network. Header-injection safety: every address and the subject
is checked for CR/LF/control characters before it reaches an ``EmailMessage``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from sales0.integrations.gmail.errors import InvalidMessage

# Gmail's hard limit for a whole message (including attachments).
MAX_MESSAGE_BYTES = 25 * 1024 * 1024

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ADDRESS = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    mime: str = "application/octet-stream"


@dataclass(frozen=True)
class DraftContent:
    """A message as the human reviewed it. ``sha256`` is what an approval binds to."""

    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    text_body: str
    html_body: str | None
    attachments: tuple[Attachment, ...]

    @property
    def sha256(self) -> str:
        return content_sha256(self)


def validate_address(address: str) -> str:
    """Plain ``local@domain`` only (no display names, no groups). Returns it stripped."""
    if not isinstance(address, str) or _CONTROL.search(address):
        raise InvalidMessage("email address contains a control character (CR/LF are never allowed)")
    value = address.strip()
    if not value or len(value) > 254 or not _ADDRESS.match(value) or ".." in value:
        raise InvalidMessage(f"not a valid email address: {value[:60]!r}")
    return value


def _validate_header_text(name: str, value: str) -> str:
    if _CONTROL.search(value):
        raise InvalidMessage(f"{name} contains a control character (CR/LF are never allowed)")
    return value


def _norm_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def normalise(
    to: list[str] | tuple[str, ...],
    cc: list[str] | tuple[str, ...],
    subject: str,
    text_body: str,
    html_body: str | None,
    attachments: list[Attachment] | tuple[Attachment, ...],
) -> DraftContent:
    """Validate and canonicalise the inputs (the same shape ``parse_raw`` yields)."""
    if not to:
        raise InvalidMessage("a message needs at least one recipient")
    subject = _validate_header_text("subject", subject)
    atts = []
    for att in attachments:
        _validate_header_text("attachment filename", att.filename)
        if not att.filename or "/" in att.filename or "\\" in att.filename:
            raise InvalidMessage(f"invalid attachment filename: {att.filename!r}")
        mime = att.mime.strip().lower()
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime):
            raise InvalidMessage(f"invalid attachment mime type: {att.mime!r}")
        atts.append(Attachment(att.filename, bytes(att.data), mime))
    return DraftContent(
        to=tuple(validate_address(a) for a in to),
        cc=tuple(validate_address(a) for a in cc),
        subject=subject,
        text_body=_norm_text(text_body),
        html_body=_norm_text(html_body) if html_body is not None else None,
        attachments=tuple(atts),
    )


def build_message(content: DraftContent) -> bytes:
    """Render RFC 2822 bytes. Raises ``InvalidMessage`` over Gmail's 25 MB limit."""
    msg = EmailMessage(policy=policy.SMTP)
    msg["To"] = ", ".join(content.to)
    if content.cc:
        msg["Cc"] = ", ".join(content.cc)
    msg["Subject"] = content.subject
    msg.set_content(content.text_body + "\n")
    if content.html_body is not None:
        msg.add_alternative(content.html_body + "\n", subtype="html")
    for att in content.attachments:
        maintype, subtype = att.mime.split("/", 1)
        msg.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.filename)
    raw = msg.as_bytes()
    if len(raw) > MAX_MESSAGE_BYTES:
        raise InvalidMessage(
            f"message is {len(raw) / 1024 / 1024:.1f} MB; Gmail's limit is 25 MB — "
            "remove or shrink the attachments"
        )
    return raw


def parse_raw(raw: bytes) -> DraftContent:
    """Parse stored RFC 2822 bytes back into the canonical content (tolerant of Gmail's re-encoding)."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    def addrs(header: str) -> tuple[str, ...]:
        out: list[str] = []
        for value in msg.get_all(header, []):
            out.extend(a.addr_spec for a in value.addresses)
        return tuple(out)

    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    atts = []
    for part in msg.iter_attachments():
        atts.append(
            Attachment(
                part.get_filename() or "attachment",
                part.get_content() if part.get_content_maintype() != "text" else part.get_content().encode(),
                part.get_content_type(),
            )
        )
    # iter_attachments yields raw bytes for non-text parts via get_content(); text parts are re-encoded.
    return DraftContent(
        to=addrs("To"),
        cc=addrs("Cc"),
        subject=str(msg.get("Subject", "")),
        text_body=_norm_text(plain.get_content()) if plain is not None else "",
        html_body=_norm_text(html.get_content()) if html is not None else None,
        attachments=tuple(atts),
    )


def content_sha256(content: DraftContent) -> str:
    """Hash of the *meaning* of the message (recipients, subject, bodies, attachment bytes).

    Not of the raw bytes: Gmail adds Message-ID/Date and may re-encode transfer encodings
    when it stores a draft, so a raw hash would never match after a round trip.
    """
    canonical = {
        "to": [a.lower() for a in content.to],
        "cc": [a.lower() for a in content.cc],
        "subject": content.subject,
        "text": content.text_body,
        "html": content.html_body,
        "attachments": [
            [a.filename, a.mime, hashlib.sha256(a.data).hexdigest()] for a in content.attachments
        ],
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

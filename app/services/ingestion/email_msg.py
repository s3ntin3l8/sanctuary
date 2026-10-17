"""Outlook .msg attachments, rewritten as RFC 822 so they ingest like any mail."""

from __future__ import annotations

import io
import mimetypes
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime

OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_MAX_EMBEDDED = 5  # .msg inside .msg inside … is refused beyond this


class MsgUnreadable(ValueError):
    """The bytes are not a readable Outlook message."""


def is_msg(filename: str | None, mime_type: str, data: bytes) -> bool:
    named = (filename or "").lower().endswith(".msg") or mime_type in (
        "application/vnd.ms-outlook",
        "application/x-msg",
    )
    return named and data.startswith(OLE_MAGIC)


def _line(value: object) -> str:
    """A header value on one line (Outlook fields can carry line breaks)."""
    return " ".join(str(value or "").split())


def _to_email(message, depth: int) -> EmailMessage:
    if depth > _MAX_EMBEDDED:
        raise MsgUnreadable("Outlook message nesting limit reached")
    out = EmailMessage()
    for header, value in (
        ("From", message.sender),
        ("To", message.to),
        ("Cc", message.cc),
        ("Subject", message.subject),
        ("Message-ID", message.messageId),
    ):
        if _line(value):
            out[header] = _line(value)
    sent = message.date
    if isinstance(sent, datetime):
        out["Date"] = format_datetime(sent)
    elif _line(sent):
        out["Date"] = _line(sent)

    body = message.body
    html = message.htmlBody
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    if body and body.strip():
        out.set_content(body)
    elif html and html.strip():
        out.set_content(html, subtype="html")
    else:
        out.set_content("")

    for attachment in message.attachments:
        data = attachment.data
        if hasattr(data, "attachments"):  # an embedded Outlook message
            embedded = _to_email(data, depth + 1)
            out.add_attachment(embedded)
            continue
        if not isinstance(data, bytes) or not data:
            continue
        name = (
            getattr(attachment, "longFilename", None)
            or getattr(attachment, "shortFilename", None)
            or getattr(attachment, "name", None)
            or "attachment"
        )
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        out.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=_line(name),
        )
    return out


def msg_to_rfc822(data: bytes) -> bytes:
    """RFC 822 bytes of an Outlook message; raises ``MsgUnreadable``."""
    import extract_msg

    try:
        message = extract_msg.openMsg(io.BytesIO(data))
    except Exception as exc:  # extract_msg raises many types for bad input
        raise MsgUnreadable(str(exc)) from exc
    try:
        if not hasattr(message, "attachments"):
            raise MsgUnreadable("Not an Outlook mail message")
        return _to_email(message, 0).as_bytes()
    except MsgUnreadable:
        raise
    except Exception as exc:
        raise MsgUnreadable(str(exc)) from exc
    finally:
        message.close()

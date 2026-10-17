import base64
import binascii
import email
import email.utils
import logging
import mimetypes
import quopri
import re
import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from email.policy import default
from zoneinfo import ZoneInfo

from markdownify import markdownify

from app import config
from app.config import TIMEZONE
from app.core.timezone import ensure_utc
from app.services.ingestion.archive_guard import ArchiveRejected
from app.services.ingestion.email_archive import archive_members, is_zip

logger = logging.getLogger(__name__)

# Matches beA/court-email attachment manifest lines:
# "SCHR_ LG IN V_ 26_05_26.PDF: 26.05.2026 08:24 - "Landgericht Ingolstadt""
_MANIFEST_LINE_RE = re.compile(
    r"^\s*(?:>\s*)*(?:[-*]\s+)?(.+?\.\w{2,5}):\s*"
    r"(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{2}:\d{2}(?::\d{2})?(?:\s*[+-]\d{2}:?\d{2}|\s*Z)?)"
    r'\s*[-–—]\s*["“„«]([^"”»]+)["”»]',
    re.MULTILINE,
)
# Boilerplate salutation/sign-off lines to strip from the note
_BOILERPLATE_RE = re.compile(
    r"^\s*(sehr geehrte[rs]?|mit freundlichen|mfg|hochachtungsvoll|"
    r"mit freundlichem|mit kollegialem|beste gr[uü][sß]e|"
    r"viele gr[uü][sß]e|liebe gr[uü][sß]e|"
    r"bitte.{0,60}nicht.{0,30}antworten|"
    r"diese e-?mail|diese nachricht|automatisch|"
    r"confidential|vertraulich|disclaimer)\b",
    re.IGNORECASE,
)


_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


def parse_message_ids(header: str | None) -> list[str]:
    """Extract the ``<msg-id>`` tokens from an In-Reply-To / References header.

    Brackets are kept so the tokens compare equal to a stored ``Message-ID``
    header. Folding whitespace, comments and stray text between ids are
    ignored; order is preserved and duplicates dropped.
    """
    if not header:
        return []
    ids = list(dict.fromkeys(_MESSAGE_ID_RE.findall(str(header))))
    if not ids:
        logger.debug("Threading header has no <msg-id> token: %r", str(header)[:120])
    return ids


def parse_email_date(date_str: str) -> datetime | None:
    """Parse RFC 5322 date string to a timezone-aware (UTC) datetime object.

    A naive result from any fallback branch is tagged UTC rather than
    converted from local time — a date-only header like "26.05.2026" means
    that calendar date, and converting via local-time interpretation would
    shift it across midnight depending on host timezone. The API serialises
    the value with its UTC offset, so display is unaffected.
    """
    if not date_str:
        return None
    try:
        return ensure_utc(email.utils.parsedate_to_datetime(date_str))
    except Exception:
        pass
    patterns = [
        r"%d %b %Y %H:%M:%S",
        r"%d %B %Y %H:%M:%S",
        r"%Y-%m-%d %H:%M:%S",
        r"%Y-%m-%d",
    ]
    date_str = date_str.strip()
    date_str = re.sub(r"[\+\-]\d{4}\s*$", "", date_str)
    date_str = re.sub(r"\s+\([^)]+\)$", "", date_str)
    for pattern in patterns:
        try:
            return ensure_utc(datetime.strptime(date_str, pattern))
        except ValueError:
            continue
    match = re.search(r"(\d{1,2})[\.\-](\d{1,2})[\.\-](\d{2,4})", date_str)
    if match:
        try:
            day, month, year = match.groups()
            if len(year) == 2:
                year = "20" + year if int(year) < 50 else "19" + year
            return ensure_utc(datetime(int(year), int(month), int(day)))
        except ValueError:
            pass
    return None


def _parse_attachment_manifest(body: str) -> list[dict]:
    """Extract the structured attachment list from an email body.

    Parses lines of the form produced by beA and German court email systems:
      SCHR_ LG IN V_ 26_05_26.PDF: 26.05.2026 08:24 - "Landgericht Ingolstadt"

    Returns a list of dicts with keys: filename, timestamp (ISO), source_label.
    Returns [] when no manifest entries are found.
    """
    # Join a wrapped timestamp/source, preserving separate filename lines.
    text = re.sub(r"(?m)(\.\w{2,5}:)\s*\n[>\s]*", r"\1 ", body)
    text = re.sub(r"(?m)(\d{2}:\d{2}(?::\d{2})?)\s*\n[>\s]*([-–—])", r"\1 \2", text)
    entries = []
    for m in _MANIFEST_LINE_RE.finditer(text):
        filename, raw_ts, source_label = (v.strip() for v in m.groups())
        timestamp = None
        issue = None
        try:
            raw_ts = re.sub(r"\s+", " ", raw_ts)
            fmt = (
                "%d.%m.%Y %H:%M:%S"
                if re.search(r"\d{2}:\d{2}:\d{2}", raw_ts)
                else "%d.%m.%Y %H:%M"
            )
            offset = re.search(r"\s*([+-]\d{2}:?\d{2}|Z)$", raw_ts)
            if offset:
                value = datetime.strptime(
                    raw_ts[: offset.start()].strip() + offset[1], fmt + "%z"
                )
            else:
                naive = datetime.strptime(raw_ts, fmt)
                zone = ZoneInfo(TIMEZONE)
                value = naive.replace(tzinfo=zone)
                other = naive.replace(tzinfo=zone, fold=1)
                if (
                    value.utcoffset() != other.utcoffset()
                    or value.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
                    != naive
                ):
                    raise ValueError("Ambiguous/nonexistent local time")
            timestamp = value.astimezone(UTC).isoformat()
        except ValueError:
            issue = "invalid_manifest_timestamp"
        # Entries below quoted reply headers can describe older deliveries.
        before = text[: m.start()]
        historical = strip_quoted_history(before) != before.strip() and bool(
            before.strip()
        )
        entries.append(
            {
                "filename": filename,
                "timestamp": timestamp,
                "source_label": source_label,
                "raw_timestamp": raw_ts,
                "historical": historical,
                "issue": issue,
            }
        )
    return entries


def _extract_email_note(body: str) -> str:
    """Extract the substantive forwarding note from an email body.

    Strips the Anlagen: manifest block, boilerplate salutations/sign-offs,
    and blank lines. Preserves the complete current note. Returns "" when nothing substantive
    remains.
    """
    # Remove Anlagen: block — find the header and skip all following manifest lines
    lines = current_text(body).splitlines()
    filtered: list[str] = []
    in_anlagen = False
    for line in lines:
        stripped = line.strip()
        header = re.match(r"^anlagen?\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if header:
            in_anlagen = True
            # A one-line "Anlagen: Schriftsatz vom …" is content, not a manifest.
            if header.group(1) and not _MANIFEST_LINE_RE.match(header.group(1)):
                filtered.append(stripped)
            continue
        if in_anlagen:
            # Manifest lines match the pattern or are blank continuation lines
            if _MANIFEST_LINE_RE.match(stripped) or not stripped:
                continue
            # First non-manifest, non-blank line ends the Anlagen block
            in_anlagen = False
        if not stripped:
            continue
        if _BOILERPLATE_RE.match(stripped):
            continue
        filtered.append(stripped)

    return " ".join(filtered).strip()


_SIGNATURE_TYPES = {
    "application/pkcs7-signature",
    "application/x-pkcs7-signature",
    "application/pgp-signature",
}
_OPAQUE_TYPES = {"application/pkcs7-mime", "application/x-pkcs7-mime"}
_ENCRYPTED_REASON = "Encrypted or opaque message part cannot be read"


def _opaque_kind(part) -> str | None:
    """ "signature" for a detached signature, "encrypted" for a body we cannot read."""
    content_type = part.get_content_type()
    name = (part.get_filename() or "").lower()
    if content_type in _SIGNATURE_TYPES or name.endswith((".p7s", ".asc", ".sig")):
        return "signature"
    if content_type in _OPAQUE_TYPES or name.endswith((".p7m", ".pgp", ".gpg")):
        return "encrypted"
    return None


def _encoded_size(part) -> int:
    """Bytes the part will decode to, estimated from its still-encoded payload."""
    payload = part.get_payload()
    if not isinstance(payload, str):
        return 0
    encoding = str(part.get("Content-Transfer-Encoding", "")).lower()
    return len(payload) * 3 // 4 if encoding == "base64" else len(payload)


def _payload(part) -> bytes | None:
    """The decoded payload, or None when it is larger than one file may be.

    Checked on the encoded form so an oversized attachment is never decoded.
    """
    if _encoded_size(part) > config.MAX_FILE_SIZE_MB * 1024 * 1024:
        return None
    return part.get_payload(decode=True) or b""


def _decode_text(part) -> str:
    """Decode a text part with its declared charset.

    Falls back to Latin-1 (never drops characters) for an unknown charset or
    bytes that don't fit the declared one.
    """
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes) or not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload.decode("latin-1")


# First line of the quoted history of a reply: "Am … schrieb …:", "On … wrote:",
# a divider, or the start of an Outlook "Von: / Gesendet:" header block.
_REPLY_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"(?:am|on)\b.{0,200}\b(?:schrieb|wrote)\b.*:?\s*$"
    r"|-{2,}\s*(?:original message|ursprüngliche nachricht|urspr\.? nachricht|"
    r"weitergeleitete nachricht|forwarded message)\s*-{2,}"
    r"|_{10,}\s*$"
    r")",
    re.IGNORECASE,
)
_OUTLOOK_HEADER_RE = re.compile(r"^\s*(?:von|from)\s*:", re.IGNORECASE)
_OUTLOOK_FOLLOW_RE = re.compile(r"^\s*(?:gesendet|sent|datum|date)\s*:", re.IGNORECASE)


def current_text(body: str) -> str:
    """The new text of a reply, without the quoted thread below it.

    Cuts at the first reply-header line (or Outlook From/Sent block) and drops
    ``>``-quoted lines. Returns "" for a pure forward; callers that need the
    text anyway use ``strip_quoted_history``.
    """
    lines = body.splitlines()
    kept: list[str] = []
    for i, line in enumerate(lines):
        if _REPLY_HEADER_RE.match(line):
            break
        if _OUTLOOK_HEADER_RE.match(line) and any(
            _OUTLOOK_FOLLOW_RE.match(nxt) for nxt in lines[i + 1 : i + 4]
        ):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    stripped = "\n".join(kept).strip()
    return stripped


def strip_quoted_history(body: str) -> str:
    return current_text(body) or body


_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("januar", "jan", "january", "jänner"),
            ("februar", "feb", "february"),
            ("märz", "mär", "mrz", "march", "mar"),
            ("april", "apr"),
            ("mai", "may"),
            ("juni", "jun", "june"),
            ("juli", "jul", "july"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("oktober", "okt", "october", "oct"),
            ("november", "nov"),
            ("dezember", "dez", "december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_ZONE = r"(?:\s*(Z|GMT|UTC|[+-]\d{2}:?\d{2})(?![\w:]))?"
_TIME = r"(?:\s*(?:um|at|,)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?" + _ZONE + ")?"
_DAY_MONTH_YEAR = re.compile(
    r"(\d{1,2})\.?\s+([A-Za-zäöüÄÖÜ]+)\.?,?\s+(\d{4})" + _TIME, re.IGNORECASE
)
_MONTH_DAY_YEAR = re.compile(
    r"([A-Za-zäöüÄÖÜ]+)\.?\s+(\d{1,2}),?\s+(\d{4})" + _TIME, re.IGNORECASE
)
_NUMERIC_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})" + _TIME)
_ISO_DATE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?"
    + _ZONE.replace("(Z|GMT|UTC|", "(Z|")
)


def _zone_offset(zone: str) -> timezone:
    if zone.upper() in ("Z", "GMT", "UTC"):
        return UTC
    digits = zone.replace(":", "")
    sign = -1 if digits[0] == "-" else 1
    return timezone(sign * timedelta(hours=int(digits[1:3]), minutes=int(digits[3:5])))


def _scan_forward_date(text: str) -> tuple[datetime, int] | None:
    """The first date in ``text`` as aware UTC, and where it ends.

    Handles "Montag, 26. Mai 2026 08:24", "26.05.2026 08:24", "Tue, May 26, 2026
    at 8:24 AM", ISO 8601 and RFC 5322. An explicit zone or offset is honoured;
    a time without one is local (``TIMEZONE``); a date without a time is that
    calendar date at 00:00 UTC.
    """
    for pattern, order in (
        (_ISO_DATE, "ymd"),
        (_DAY_MONTH_YEAR, "dmy"),
        (_MONTH_DAY_YEAR, "mdy"),
        (_NUMERIC_DATE, "dmy"),
    ):
        match = pattern.search(text)
        if not match:
            continue
        a, b, c, hour, minute, second, *rest = match.groups()
        meridian = rest[0] if order != "ymd" else None
        zone = rest[-1]
        year, month, day = (
            (a, b, c) if order == "ymd" else (c, a, b) if order == "mdy" else (c, b, a)
        )
        try:
            month_no = int(month) if month.isdigit() else _MONTHS[month.lower()]
            h = int(hour) if hour else None
            if h is not None and meridian:
                h = h % 12 + (12 if meridian.upper() == "PM" else 0)
            if h is None:
                return datetime(int(year), month_no, int(day), tzinfo=UTC), match.end()
            local = datetime(
                int(year),
                month_no,
                int(day),
                h,
                int(minute),
                int(second or 0),
                tzinfo=_zone_offset(zone) if zone else ZoneInfo(TIMEZONE),
            )
        except (KeyError, ValueError):
            continue
        return local.astimezone(UTC), match.end()
    return None


def parse_forward_date(text: str) -> datetime | None:
    """A forwarded header's date as aware UTC (see ``_scan_forward_date``)."""
    found = _scan_forward_date(re.sub(r"\s+", " ", text).strip())
    return found[0] if found else None


_FORWARD_SUBJECT_RE = re.compile(r"^\s*(?:wg|fwd?|weitergeleitet)\s*:", re.IGNORECASE)
_FORWARD_DIVIDER_RE = re.compile(
    r"^\s*-{2,}\s*(?:weitergeleitete nachricht|forwarded message)\s*-{2,}",
    re.IGNORECASE,
)
_HEADER_LINE_RE = re.compile(r"^\s*([A-Za-zäöüÄÖÜ-]+)\s*:\s*(.*)$")
# "Am 26.05.2026 um 08:24 schrieb Müller <m@x.de>:" / "On May 26, 2026 at 8:24 AM
# Müller <m@x.de> wrote:" — the attribution line of a quoted message.
_ATTRIBUTION_RE = re.compile(
    r"^\s*(?:am|on)\s+(.{0,200}?)\s*\b(?:schrieb|wrote)\b\s*(.*?):\s*$",
    re.IGNORECASE,
)


def parse_inline_forward(body: str, subject: str = "") -> dict | None:
    """Original sender and date of a pure inline forward.

    A "WG:" without a note of its own is just the forwarded block, so the
    forwarder's From/Date describe the forwarding, not the letter. Returns
    ``{"sender", "sent_at"}`` (either may be None) for the first header block of
    such a forward, and None when the mail is not a pure forward or has no
    readable header block.
    """
    if current_text(body):
        return None
    lines = body.splitlines()
    explicit = any(_FORWARD_DIVIDER_RE.match(line) for line in lines)
    if not (explicit or _FORWARD_SUBJECT_RE.match(subject)):
        return None
    for i, line in enumerate(lines):
        attribution = _ATTRIBUTION_RE.match(line)
        if attribution:
            lead, trailing = attribution.groups()
            lead = re.sub(r"\s+", " ", lead).strip()
            found = _scan_forward_date(lead)
            # German puts the sender after "schrieb"; English between the date
            # and "wrote". Without a date the English form cannot be split.
            sender = trailing.strip() or (lead[found[1] :].strip(" ,") if found else "")
            if found or sender:
                return {
                    "sender": sender or None,
                    "sent_at": found[0] if found else None,
                }
            continue
        if not _FORWARD_DIVIDER_RE.match(line):
            start = (
                i
                if _OUTLOOK_HEADER_RE.match(line)
                and any(_OUTLOOK_FOLLOW_RE.match(n) for n in lines[i + 1 : i + 4])
                else None
            )
        else:
            start = i + 1
        if start is None:
            continue
        sender, sent = None, None
        for header in lines[start : start + 12]:
            if not header.strip():
                if sender or sent:
                    break
                continue
            match = _HEADER_LINE_RE.match(header)
            if not match:
                break
            key, value = match.group(1).lower(), match.group(2).strip()
            if key in ("von", "from") and sender is None:
                value = re.sub(r"\s*\[mailto:([^\]]+)\]", r" <\1>", value)
                sender = re.sub(r"<mailto:([^>]+)>", r"<\1>", value) or None
            elif key in ("gesendet", "sent", "datum", "date") and sent is None:
                sent = parse_forward_date(value)
        if sender or sent:
            return {"sender": sender, "sent_at": sent}
    return None


def normalize_filename(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip().casefold()


def _raw_body(raw: bytes) -> bytes:
    pieces = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)
    return pieces[1] if len(pieces) == 2 else b""


def _raw_mime_children(raw: bytes, boundary: str) -> list[bytes]:
    """Keep exact encoded inner bytes; stdlib's RFC822 payload is already parsed.

    Re-serializing that payload can corrupt soft-wrapped quoted-printable
    headers before decoding them. Slice using the container's actual boundary.
    """
    payload = _raw_body(raw)
    markers = list(
        re.finditer(
            rb"(?m)^--"
            + re.escape(boundary.encode("ascii", errors="surrogateescape"))
            + rb"(--)?[ \t]*\r?$",
            payload,
        )
    )
    children = []
    for marker, following in zip(markers, markers[1:], strict=False):
        if marker[1]:
            break
        start = marker.end()
        if payload[start : start + 1] == b"\n":
            start += 1
        end = following.start()
        if payload[end - 2 : end] == b"\r\n":
            end -= 2
        elif payload[end - 1 : end] == b"\n":
            end -= 1
        children.append(payload[start:end])
    return children


def _is_auto_reply(msg) -> bool:
    """An out-of-office / autoresponder notice (RFC 3834 and the legacy headers).

    Deliberately narrow: the sender is on the allowlist, so a mail dropped by
    mistake is lost evidence. Bulk and list mail, and "auto-generated"
    notifications (a court's receipt confirmation is one), are still ingested.
    """
    if str(msg.get("Auto-Submitted", "")).strip().lower() == "auto-replied":
        return True
    if str(msg.get("Precedence", "")).strip().lower() in {"auto_reply", "junk"}:
        return True
    return any(msg.get(h) for h in ("X-Autoreply", "X-Autorespond"))


def parse_rfc822(raw_bytes: bytes) -> dict:
    """Read each attached RFC822 message as a separate transport source.

    The root fields remain convenient for upload/conversion callers. Sources
    contain only their own MIME parts; descendants never leak into their body.
    Binary payloads are transient and excluded from persisted EmailSource data.
    """
    sources: list[dict] = []

    def read_source(
        msg,
        source_id: str,
        parent_id: str | None,
        depth: int = 0,
        source_bytes: bytes = b"",
    ):
        if depth > 30:
            raise ValueError("Email nesting exceeds 30 messages")
        reply_ids = parse_message_ids(msg.get("In-Reply-To", ""))
        date = str(msg.get("Date", ""))
        sent_at = parse_email_date(date)
        source: dict = {
            "id": source_id,
            "parent_id": parent_id,
            "sender": str(msg.get("From", "")),
            **{
                key: [
                    address
                    for _, address in email.utils.getaddresses(msg.get_all(header, []))
                    if address
                ]
                for key, header in (
                    ("to", "To"),
                    ("cc", "Cc"),
                    ("bcc", "Bcc"),
                    ("reply_to", "Reply-To"),
                )
            },
            "subject": str(msg.get("Subject", "")),
            "message_id": str(msg.get("Message-ID", "")).strip(),
            "date": date,
            "sent_at": sent_at.isoformat() if sent_at else None,
            "received_date": sent_at,
            "in_reply_to": reply_ids[0] if reply_ids else None,
            "thread_refs": list(
                dict.fromkeys(parse_message_ids(msg.get("References", "")) + reply_ids)
            ),
            "parts": [],
            "attachments": [],
        }
        sources.append(source)
        plain, html = [], []
        leaves = []

        def walk(part, path, part_raw):
            if (part.get_filename() or "").lower().endswith(
                ".eml"
            ) and part.get_content_type() != "message/rfc822":
                data = part.get_payload(decode=True) or b""
                child = email.message_from_bytes(data, policy=default)
                if not any(
                    child.get(header)
                    for header in ("From", "Subject", "Date", "Message-ID")
                ):
                    record(part, path, data, "failed").update(
                        reason="Unreadable attached email"
                    )
                else:
                    nested_part = record(part, path, data, "nested_source")
                    try:
                        read_source(child, f"{path}.m0", source_id, depth + 1, data)
                    except ValueError:
                        nested_part.update(
                            outcome="failed",
                            reason="Attached email nesting limit reached",
                        )
                return
            if part.get_content_type() == "message/rfc822":
                transfer = str(part.get("Content-Transfer-Encoding", "")).lower()
                nested_part = None
                try:
                    data = _raw_body(part_raw)
                    if transfer == "base64":
                        data = base64.b64decode(
                            re.sub(rb"\s+", b"", data), validate=True
                        )
                    elif transfer == "quoted-printable":
                        data = quopri.decodestring(data)
                    if not data:
                        raise ValueError("Empty attached email")
                    child = email.message_from_bytes(data, policy=default)
                    nested_part = record(part, path, data, "nested_source")
                    read_source(child, f"{path}.m0", source_id, depth + 1, data)
                except (ValueError, binascii.Error):
                    if nested_part is None:
                        nested_part = record(part, path, part_raw, "failed")
                    nested_part.update(
                        outcome="failed",
                        reason="Invalid attached email encoding or nesting",
                    )
                return
            if part.get_content_type() == "multipart/encrypted":
                record(part, path, part_raw, "encrypted").update(
                    reason=_ENCRYPTED_REASON
                )
                return
            if part.is_multipart():
                raw_children = _raw_mime_children(part_raw, part.get_boundary() or "")
                for i, child in enumerate(part.iter_parts()):
                    child_raw = (
                        raw_children[i]
                        if i < len(raw_children)
                        else child.as_bytes(policy=default)
                    )
                    walk(child, f"{path}.{i}", child_raw)
                return
            leaves.append((part, path))
            if (
                not part.get_filename()
                and part.get_content_disposition() != "attachment"
            ):
                if part.get_content_type() == "text/html":
                    html.append(_decode_text(part))
                elif part.get_content_type() == "text/plain":
                    plain.append(_decode_text(part))

        def record(part, path, data, outcome="pending"):
            original = part.get_filename()
            suffix = (
                ".pdf"
                if data.startswith(b"%PDF")
                else (mimetypes.guess_extension(part.get_content_type()) or ".bin")
            )
            name = original or f"attachment_{path.replace('.', '_')}{suffix}"
            entry = {
                "id": path,
                "filename": name,
                "original_filename": original,
                "mime_type": part.get_content_type(),
                "disposition": part.get_content_disposition(),
                "size": len(data),
                "outcome": outcome,
                "reason": None,
                "document_ids": [],
                "content": data,
            }
            source["parts"].append(entry)
            if outcome == "pending":
                source["attachments"].append(entry)
            return entry

        def record_archive(entry, path, data):
            """A zip is accounted for as one part; its files become parts of
            this source, ingested like any other attachment."""
            try:
                members = archive_members(data)
            except ArchiveRejected as exc:
                entry.update(outcome="unsupported", reason=str(exc))
                return
            if not members:
                entry.update(outcome="unsupported", reason="Archive contains no files")
                return
            emit_members(members, path)

        def emit_members(members, base):
            for i, member in enumerate(members):
                path = f"{base}.z{i}"
                entry = {
                    "id": path,
                    "filename": member["name"],
                    "original_filename": member["name"],
                    "mime_type": mimetypes.guess_type(member["name"])[0]
                    or "application/octet-stream",
                    "disposition": "attachment",
                    "size": member["size"],
                    "outcome": member["outcome"],
                    "reason": member["reason"],
                    "document_ids": [],
                    "content": member["data"],
                }
                source["parts"].append(entry)
                if member["outcome"] == "pending":
                    source["attachments"].append(entry)
                emit_members(member["members"], path)

        walk(msg, source_id, source_bytes)
        html_body = "\n".join(html)
        for part, path in leaves:
            cid = str(part.get("Content-ID", "")).strip("<>")
            explicit = part.get_content_disposition() == "attachment"
            embedded = (
                not explicit
                and bool(cid)
                and part.get_content_maintype() == "image"
                and f"cid:{cid}" in html_body
            )
            kind = _opaque_kind(part)
            if kind:
                entry = record(part, path, _payload(part) or b"", kind)
                if kind == "encrypted":
                    entry.update(reason=_ENCRYPTED_REASON)
            elif embedded:
                record(part, path, _payload(part) or b"", "ignored_inline")
            elif (
                explicit
                or part.get_filename()
                or part.get_content_maintype() != "text"
                or part.get_content_type() == "text/calendar"
            ):
                data = _payload(part)
                if data is None:
                    entry = record(part, path, b"", "unsupported")
                    entry.update(reason="File is too large", size=_encoded_size(part))
                elif is_zip(part.get_filename(), part.get_content_type(), data):
                    record_archive(record(part, path, data, "archive"), path, data)
                else:
                    entry = record(part, path, data)
                    # An invitation travels as a sibling of the mail's own text
                    # (multipart/alternative): it must not turn that text into
                    # mere transport.
                    entry["inline_alternative"] = (
                        part.get_content_type() == "text/calendar"
                        and not part.get_filename()
                        and part.get_content_disposition() != "attachment"
                    )
        body = "\n".join(plain)
        if not body.strip() and html_body.strip():
            body = markdownify(
                html_body, heading_style="ATX", strip=["div"], escape_underscores=False
            )
        source["attachment_manifest"] = _parse_attachment_manifest(body)
        source["email_note"] = _extract_email_note(body)
        source["body"] = strip_quoted_history(body) if body else ""
        source["inline_forward"] = (
            parse_inline_forward(body, source["subject"]) if body else None
        )
        # An attached message counts as an attachment of this source as well.
        # A detached signature is not an attachment: it must not turn the
        # mail's own body into mere transport.
        source["has_attachments"] = any(
            p["outcome"] not in ("ignored_inline", "signature")
            and not p.get("inline_alternative")
            for p in source["parts"]
        )
        return source

    root_msg = email.message_from_bytes(raw_bytes, policy=default)
    root = read_source(root_msg, "0", None, source_bytes=raw_bytes)
    return {**root, "sources": sources, "auto_replied": _is_auto_reply(root_msg)}

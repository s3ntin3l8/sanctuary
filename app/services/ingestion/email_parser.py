import email
import email.utils
import re
from datetime import datetime
from email.policy import default

# Matches beA/court-email attachment manifest lines:
# "SCHR_ LG IN V_ 26_05_26.PDF: 26.05.2026 08:24 - "Landgericht Ingolstadt""
_MANIFEST_LINE_RE = re.compile(
    r"^(.+?\.\w{2,5}):\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})\s*-\s*\"([^\"]+)\"",
    re.MULTILINE,
)
# Boilerplate salutation/sign-off lines to strip from the note
_BOILERPLATE_RE = re.compile(
    r"^\s*(sehr geehrte[rs]?|mit freundlichen|mfg|hochachtungsvoll|"
    r"mit freundlichem|mit kollegialem|beste gr[uü][sß]e|"
    r"viele gr[uü][sß]e|liebe gr[uü][sß]e|"
    r"anlagen?:|bitte.{0,60}nicht.{0,30}antworten|"
    r"diese e-?mail|diese nachricht|automatisch|"
    r"confidential|vertraulich|disclaimer)\b",
    re.IGNORECASE,
)


def parse_email_date(date_str: str) -> datetime | None:
    """Parse RFC 5322 date string to datetime object."""
    if not date_str:
        return None
    try:
        return email.utils.parsedate_to_datetime(date_str)
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
            return datetime.strptime(date_str, pattern)
        except ValueError:
            continue
    match = re.search(r"(\d{1,2})[\.\-](\d{1,2})[\.\-](\d{2,4})", date_str)
    if match:
        try:
            day, month, year = match.groups()
            if len(year) == 2:
                year = "20" + year if int(year) < 50 else "19" + year
            return datetime(int(year), int(month), int(day))
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
    entries = []
    for m in _MANIFEST_LINE_RE.finditer(body):
        filename, raw_ts, source_label = (
            m.group(1).strip(),
            m.group(2).strip(),
            m.group(3).strip(),
        )
        # Convert "26.05.2026 08:24" → "2026-05-26T08:24"
        try:
            day, month, year = raw_ts[:10].split(".")
            time_part = raw_ts[11:].strip()
            iso_ts = f"{year}-{month}-{day}T{time_part}"
        except Exception:
            iso_ts = raw_ts
        entries.append(
            {"filename": filename, "timestamp": iso_ts, "source_label": source_label}
        )
    return entries


def _extract_email_note(body: str) -> str:
    """Extract the substantive forwarding note from an email body.

    Strips the Anlagen: manifest block, boilerplate salutations/sign-offs,
    and blank lines. Truncates to 800 chars. Returns "" when nothing substantive
    remains.
    """
    # Remove Anlagen: block — find the header and skip all following manifest lines
    lines = body.splitlines()
    filtered: list[str] = []
    in_anlagen = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^anlagen?\s*:", stripped, re.IGNORECASE):
            in_anlagen = True
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

    note = " ".join(filtered).strip()
    if len(note) > 800:
        note = note[:800].rsplit(" ", 1)[0] + " …"
    return note


def parse_rfc822(raw_bytes: bytes) -> dict:
    msg = email.message_from_bytes(raw_bytes, policy=default)

    body = ""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            content_disposition = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()
            content_id = part.get("Content-ID", "")
            # Parts with a Content-ID are inline-embedded (logos, signatures) — skip.
            is_attachment = not content_id and (
                "attachment" in content_disposition
                or (
                    filename
                    and part.get_content_maintype() not in ("text", "multipart")
                )
            )
            if is_attachment:
                attachments.append(
                    {
                        "filename": filename,
                        "content": part.get_payload(decode=True),
                    }
                )
            elif (
                part.get_content_type() == "text/plain"
                and "attachment" not in content_disposition
            ):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes) and payload:
                    body += payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes) and payload:
            body = payload.decode(errors="ignore")

    attachment_manifest = _parse_attachment_manifest(body) if body else []
    email_note = _extract_email_note(body) if body else ""

    return {
        "sender": msg.get("From", ""),
        "subject": msg.get("Subject", ""),
        "message_id": msg.get("Message-ID", ""),
        "date": msg.get("Date", ""),
        "received_date": parse_email_date(msg.get("Date", "")),
        "body": body,
        "attachments": attachments,
        "reply_to": msg.get("Reply-To", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "references": msg.get("References", ""),
        "attachment_manifest": attachment_manifest,
        "email_note": email_note,
    }

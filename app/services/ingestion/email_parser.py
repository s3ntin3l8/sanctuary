import email
import email.utils
import re
from datetime import datetime
from email.policy import default


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
                if payload:
                    body += payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")

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
    }

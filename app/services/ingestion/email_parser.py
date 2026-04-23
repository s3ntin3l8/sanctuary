import email
from email.policy import default


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
            is_attachment = "attachment" in content_disposition or (
                filename and part.get_content_maintype() not in ("text", "multipart")
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
        "body": body,
        "attachments": attachments,
    }

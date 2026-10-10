"""Cross-checks on extracted metadata that no single OCR or AI read can make.

A model that misreads a handwritten "28-08-2026" as 2016 is confident and
internally consistent; only the surrounding bundle shows it is off.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.models.database import Document, IngestBatch

# Time zones and a letter written the evening before it arrives.
_FUTURE_SLACK = timedelta(days=1)


def _one_digit_apart(a: int, b: int) -> bool:
    """Same-length numbers that differ in exactly one digit (2016 vs 2026)."""
    sa, sb = str(a), str(b)
    return len(sa) == len(sb) and sum(x != y for x, y in zip(sa, sb, strict=True)) == 1


def issued_date_suspect(doc: Document, db: Session) -> bool:
    """The extracted issue date cannot be right given where the document sits.

    Two tells, both about the date and not about the document's content:
    - it lies after the day the document arrived (a letter cannot be issued
      after it was received), or
    - it falls on the same day and month as the bundle's cover letter or the
      arrival day but a year that differs in a single digit — the signature
      of a misread digit on a form signed the day it was sent.

    A date the user typed or confirmed is never second-guessed.
    """
    issued = doc.issued_date
    if (
        issued is None
        or (doc.extraction_confidence or {}).get("issued_date") == "user_set"
    ):
        return False
    day = issued.date()

    batch = db.get(IngestBatch, doc.ingest_batch_id) if doc.ingest_batch_id else None
    arrived = doc.received_date or (batch.received_at if batch else None)
    if arrived is not None and day > arrived.date() + _FUTURE_SLACK:
        return True

    lead = db.get(Document, doc.parent_id) if doc.parent_id else None
    references = [
        r.date() for r in (lead.issued_date if lead else None, arrived) if r is not None
    ]
    return any(
        (ref.month, ref.day) == (day.month, day.day)
        and _one_digit_apart(ref.year, day.year)
        for ref in references
    )

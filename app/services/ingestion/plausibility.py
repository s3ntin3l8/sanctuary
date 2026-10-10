"""Cross-checks on extracted metadata that no single OCR or AI read can make.

A model that misreads a handwritten "28-08-2026" as 2016 is confident and
internally consistent; only the surrounding bundle shows it is off.
"""

import re
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models.database import Document, IngestBatch

# Slack between a letter's issue date and the day *it* arrived (time zones, a
# letter dated the evening before it was delivered). Never compared with the
# wall clock: an old document is not "in the future" because of today's date.
_FUTURE_SLACK = timedelta(days=1)

# A document delivered in a bundle is rarely dated years before the cover
# letter / arrival day. A date this much older is nearly always a misread or a
# date of birth picked off the parties block — flagged for a look, not
# rejected (an old bank statement enclosed as evidence is legitimate).
_STALE_AFTER = timedelta(days=5 * 365)


def _one_digit_apart(a: int, b: int) -> bool:
    """Same-length numbers that differ in exactly one digit (2016 vs 2026)."""
    sa, sb = str(a), str(b)
    return len(sa) == len(sb) and sum(x != y for x, y in zip(sa, sb, strict=True)) == 1


def issued_date_suspect(doc: Document, db: Session) -> bool:
    """The extracted issue date cannot be right given where the document sits.

    Three tells, all about the date and not about the document's content:
    - it lies after the day the document arrived (a letter cannot be issued
      after it was received),
    - it is more than five years older than the cover letter or the arrival
      day (a birth date or a century-digit misread), or
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

    # The cover letter's date is the best reference; the arrival day is the
    # catch-all when the document has no parent or the parent has no date.
    lead = db.get(Document, doc.parent_id) if doc.parent_id else None
    references = [
        r.date() for r in (lead.issued_date if lead else None, arrived) if r is not None
    ]
    if any(day < ref - _STALE_AFTER for ref in references):
        return True
    return any(
        (ref.month, ref.day) == (day.month, day.day)
        and _one_digit_apart(ref.year, day.year)
        for ref in references
    )


# A canonical Aktenzeichen (see ``normalize_az_court``): department, register,
# serial/year, optional suffix. "3 F 2022/23" -> ("3", ("F", "2022", "23")).
_AZ_PARTS_RE = re.compile(r"^(\d+)\s([A-Z]{1,3})\s(\d+)/(\d+)(?:\s[A-Z]{1,3})?$")


def _az_parts(az: str | None) -> tuple[str, tuple[str, str, str]] | None:
    m = _AZ_PARTS_RE.match(az or "")
    return (m.group(1), (m.group(2), m.group(3), m.group(4))) if m else None


def az_department_variants(a: str | None, b: str | None) -> bool:
    """Same register, serial and year under two different department numbers.

    "2 F 2022/23" vs "3 F 2022/23" is either an OCR misread of the department
    or a case that moved between departments; only a person can tell.
    """
    pa, pb = _az_parts(a), _az_parts(b)
    return bool(pa and pb and pa[1] == pb[1] and pa[0] != pb[0])


def _az_confirmed(confidence: dict | None) -> bool:
    return (confidence or {}).get("az_court") == "user_set"


def az_conflict(doc: Document, db: Session) -> bool:
    """Another document of the bundle carries this Aktenzeichen under a different department.

    Matching is exact, so one misread department splits the ``Via=thread``
    pool and can seed a second proceeding. A person who typed or confirmed
    either side has already looked at it, so neither document is flagged then.
    """
    if (
        not doc.az_court
        or not doc.ingest_batch_id
        or _az_confirmed(doc.extraction_confidence)
    ):
        return False
    siblings = (
        db.query(Document.az_court, Document.extraction_confidence)
        .filter(
            Document.ingest_batch_id == doc.ingest_batch_id,
            Document.id != doc.id,
            Document.az_court.isnot(None),
        )
        .all()
    )
    return any(
        not _az_confirmed(conf) and az_department_variants(doc.az_court, az)
        for az, conf in siblings
    )

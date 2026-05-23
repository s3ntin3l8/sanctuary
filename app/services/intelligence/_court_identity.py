"""Shared helpers for "is this document a confirmed court source?" checks.

Used by guards across the intelligence pipeline (batch_analyzer originator
override, ai_summary metadata writeback, etc.) to refuse classifying a
clearly-court document as a party-side document. Centralized here so the
court-name keyword list lives in one place.
"""

from app.models.database import Document
from app.models.enums import DocumentType

_COURT_SENDER_FRAGMENTS = (
    "amtsgericht",
    "landgericht",
    "oberlandesgericht",
    "bundesgerichtshof",
    "bundesverfassungsgericht",
    "verwaltungsgericht",
    "sozialgericht",
    "arbeitsgericht",
    "finanzgericht",
)


def is_court_name(name: str | None) -> bool:
    """Return True when a name string contains a recognized German court term.

    Substring match against `_COURT_SENDER_FRAGMENTS`, case-insensitive.
    Useful both for sender fields on Document and for free-text party names
    on case.opposing_parties / case.parties.
    """
    if not name:
        return False
    lower = name.lower()
    return any(fragment in lower for fragment in _COURT_SENDER_FRAGMENTS)


def is_confirmed_court_document(doc: Document) -> bool:
    """Return True when static metadata confirms a court is the author.

    Guards downstream AI-driven overrides from downgrading
    Phase-1-COURT documents back to a party type. RULING (Beschluss/Urteil)
    and RELAY (Begleitschreiben) are court-only document types — only courts
    issue rulings or relay correspondence. A sender naming a recognized German
    court institution is also a reliable signal.
    """
    if doc.document_type in (DocumentType.RULING, DocumentType.RELAY):
        return True
    return is_court_name(doc.sender)

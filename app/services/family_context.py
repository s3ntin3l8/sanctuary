"""A document does not stand alone: it sits in a family (bundle, sub-group,
parent/child tree). Helpers here answer questions about that family so the
triage view, the pipeline and the review UI agree on them."""

from app.models.database import Document
from app.models.enums import DocumentType


def is_relay(doc: Document) -> bool:
    """True when the document is a court wrapper around someone else's filing.

    A relay is a pure transmission: its substance lives in what it forwards,
    so it should not own the identity of the group it heads.
    """
    return doc.document_type == DocumentType.RELAY or bool(doc.court_relay)

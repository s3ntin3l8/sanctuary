"""Wire cover letter + enclosures for scan-confirmed slices."""

from sqlalchemy.orm import Session

from app.models.enums import DocumentRole


def wire_cover_letter(
    db: Session,
    cover_doc_id: int,
    child_doc_ids: list[int],
    *,
    court_relay: bool,
) -> None:
    """Mark cover_doc as COVER_LETTER and wire children as ENCLOSUREs.

    All docs must already exist in the session; caller must commit.
    """
    from app.models.database import Document

    cover = db.get(Document, cover_doc_id)
    if not cover:
        return
    cover.role = DocumentRole.COVER_LETTER
    cover.court_relay = court_relay

    for child_id in child_doc_ids:
        child = db.get(Document, child_id)
        if child:
            child.role = DocumentRole.ENCLOSURE
            child.parent_id = cover_doc_id

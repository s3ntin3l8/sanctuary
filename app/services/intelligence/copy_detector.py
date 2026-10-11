"""Spot documents that are copies of ones already in the system.

An opposing party attaches earlier rulings and motions to its filings as
exhibits. Those slices are evidence the other side submitted, but they are not
new correspondence: they must not raise claims, deadlines or relationships of
their own, and the timeline and graph should show one document, not two.

A copy is found by text, not by file bytes (a scan of a printout shares none):
two documents of the same owner, dated the same day and tied by sender, court
file number or firm reference, whose word 5-gram shingles overlap by at least
``COPY_CONTAINMENT_MIN`` of the shorter one. Containment (not Jaccard) so that
a stamp, a header or a trailing page does not break the match.

The link is an ordinary reviewable edge (``COPY_OF``, copy -> original,
``ai_detected``); rejecting it is honoured. Nothing here commits.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.database import (
    ClaimEvidenceProposal,
    Document,
    DocumentRelationship,
)
from app.models.enums import (
    DocumentRole,
    DocumentStatus,
    PipelineStage,
    ProposalStatus,
    RelationshipConfidence,
    RelationshipType,
)
from app.repositories.claim import ClaimRepository
from app.repositories.document_relationship import insert_edge_if_absent

# Tune on real scans (OCR noise lowers overlap between a clean PDF and a scanned
# copy of it); see the follow-up issue.
COPY_CONTAINMENT_MIN = 0.85
# Too few shingles and a short form letter "matches" every sibling.
_MIN_SHINGLES = 20
_SHINGLE_WORDS = 5
_MAX_CANDIDATES = 20

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_NON_WORD_RE = re.compile(r"[^\w]+")


def _shingles(text: str | None) -> set[int]:
    words = _NON_WORD_RE.sub(" ", _IMAGE_RE.sub(" ", (text or "").lower())).split()
    if len(words) < _SHINGLE_WORDS:
        return set()
    return {
        hash(tuple(words[i : i + _SHINGLE_WORDS]))
        for i in range(len(words) - _SHINGLE_WORDS + 1)
    }


def containment(a: set[int], b: set[int]) -> float:
    """|A ∩ B| / min(|A|, |B|); 0 for an empty side."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _original_first(a: Document, b: Document) -> tuple[Document, Document]:
    """(original, copy): a free-standing letter over an enclosure, then the one
    that arrived first, then the lower id."""

    def key(d: Document):
        return (
            d.role == DocumentRole.ENCLOSURE,
            d.ingest_date.timestamp() if d.ingest_date else float("inf"),
            d.id,
        )

    return (a, b) if key(a) <= key(b) else (b, a)


def is_copy(db: Session, doc_id: int) -> bool:
    """True when ``doc_id`` has a (not rejected) COPY_OF edge to an original."""
    return (
        db.query(DocumentRelationship.id)
        .filter(
            DocumentRelationship.from_document_id == doc_id,
            DocumentRelationship.relationship_type == RelationshipType.COPY_OF,
        )
        .first()
        is not None
    )


def _candidates(db: Session, doc: Document) -> list[Document]:
    if doc.issued_date is None:
        return []
    ties = []
    if doc.sender:
        ties.append(
            func.lower(func.trim(Document.sender)) == doc.sender.strip().lower()
        )
    if doc.az_court:
        ties.append(Document.az_court == doc.az_court)
    if doc.internal_id:
        ties.append(Document.internal_id == doc.internal_id)
    if not ties:
        return []
    day = doc.issued_date.replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(Document).filter(
        Document.id != doc.id,
        Document.status != DocumentStatus.DISMISSED,
        Document.content.isnot(None),
        Document.issued_date >= day,
        Document.issued_date < day + timedelta(days=1),
        or_(*ties),
    )
    q = q.filter(
        Document.owner_id.is_(None)
        if doc.owner_id is None
        else Document.owner_id == doc.owner_id
    )
    if doc.ingest_batch_id is not None:
        q = q.filter(
            or_(
                Document.ingest_batch_id.is_(None),
                Document.ingest_batch_id != doc.ingest_batch_id,
            )
        )
    return q.order_by(Document.id).limit(_MAX_CANDIDATES).all()


@dataclass
class CopyOutcome:
    original_id: int | None = None  # set when ``doc`` is a copy of this document
    copy_ids: list[int] = field(default_factory=list)  # documents now copies of ``doc``


def detect_and_link(db: Session, doc: Document) -> CopyOutcome:
    """Link ``doc`` as a copy of an earlier document, and any earlier-processed
    documents that turn out to be copies of ``doc`` (the original can arrive
    after its copy). Idempotent; honours rejected edges."""
    outcome = CopyOutcome()
    mine = _shingles(doc.content)
    if len(mine) < _MIN_SHINGLES:
        return outcome

    best_original: tuple[float, Document] | None = None
    for cand in _candidates(db, doc):
        theirs = _shingles(cand.content)
        if len(theirs) < _MIN_SHINGLES:
            continue
        score = containment(mine, theirs)
        if score < COPY_CONTAINMENT_MIN:
            continue
        original, copy = _original_first(doc, cand)
        if copy is doc:
            if best_original is None or score > best_original[0]:
                best_original = (score, original)
        elif not is_copy(db, cand.id) and insert_edge_if_absent(
            db,
            from_document_id=cand.id,
            to_document_id=doc.id,
            relationship_type=RelationshipType.COPY_OF,
            confidence=RelationshipConfidence.AI_DETECTED,
            notes=f"copy detector: containment={score:.2f}",
        ):
            outcome.copy_ids.append(cand.id)

    if best_original is not None:
        score, original = best_original
        insert_edge_if_absent(
            db,
            from_document_id=doc.id,
            to_document_id=original.id,
            relationship_type=RelationshipType.COPY_OF,
            confidence=RelationshipConfidence.AI_DETECTED,
            notes=f"copy detector: containment={score:.2f}",
        )
    if is_copy(db, doc.id):
        outcome.original_id = (
            db.query(DocumentRelationship.to_document_id)
            .filter(
                DocumentRelationship.from_document_id == doc.id,
                DocumentRelationship.relationship_type == RelationshipType.COPY_OF,
            )
            .scalar()
        )
    return outcome


def quiet_copy(db: Session, doc: Document) -> None:
    """Take a copy out of the analysis: skip its RELATIONSHIPS and CLAIMS stages
    and remove what an earlier run produced on its own (claims only it raised,
    pending suggestions, AI-detected edges, action items). Anything a person
    confirmed stays. Does not commit."""
    from app.services.intelligence.action_items import purge_action_items_from_doc
    from app.services.pipeline_status import mark_skipped

    for stage in (PipelineStage.RELATIONSHIPS, PipelineStage.CLAIMS):
        mark_skipped(doc.id, stage, db, reason="copy_of", commit=False)

    for claim in ClaimRepository(db).claims_only_originated_by_document(doc.id):
        db.delete(claim)
    db.query(ClaimEvidenceProposal).filter(
        ClaimEvidenceProposal.source_document_id == doc.id,
        ClaimEvidenceProposal.status == ProposalStatus.PENDING,
    ).delete(synchronize_session=False)
    db.query(DocumentRelationship).filter(
        or_(
            DocumentRelationship.from_document_id == doc.id,
            DocumentRelationship.to_document_id == doc.id,
        ),
        DocumentRelationship.confidence == RelationshipConfidence.AI_DETECTED,
        DocumentRelationship.relationship_type.notin_(
            [RelationshipType.ENCLOSES, RelationshipType.COPY_OF]
        ),
    ).delete(synchronize_session=False)
    purge_action_items_from_doc(doc.id, db)

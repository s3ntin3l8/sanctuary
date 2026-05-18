"""Triage dismissal + hard-delete.

Free-function module (no class). Owns the two destructive bundle operations:
- dismiss_bundle: soft-delete (marks status=DISMISSED on the batch + its docs
  + its ActionItems). Reversible by an admin.
- delete_bundle: hard-delete (removes rows + raw source file). Raises on
  mid-flight batches.

Mirrors the structure of app/api/triage/bundle_ops.py which exposes both
operations as HTTP endpoints.
"""

import logging
import os

from sqlalchemy.orm import Session

from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import ActionItemStatus, DocumentStatus, IngestBatchStatus

logger = logging.getLogger(__name__)


def dismiss_bundle(
    db: Session, batch_id: int | None = None, doc_id: int | None = None
) -> bool:
    """Mark a batch or loose document (and children) as DISMISSED."""
    if batch_id:
        batch = db.get(IngestBatch, batch_id)
        if batch:
            batch.status = IngestBatchStatus.DISMISSED
            db.query(Document).filter(Document.ingest_batch_id == batch_id).update(
                {"status": DocumentStatus.DISMISSED}, synchronize_session=False
            )
            doc_ids = (
                db.query(Document.id).filter(Document.ingest_batch_id == batch_id).all()
            )
            doc_id_list = [d[0] for d in doc_ids]
            if doc_id_list:
                db.query(ActionItem).filter(
                    ActionItem.source_document_id.in_(doc_id_list)
                ).update(
                    {"status": ActionItemStatus.DISMISSED},
                    synchronize_session=False,
                )
            db.commit()
            return True
    elif doc_id:
        doc = db.get(Document, doc_id)
        if doc:
            doc.status = DocumentStatus.DISMISSED
            db.query(ActionItem).filter(ActionItem.source_document_id == doc_id).update(
                {"status": ActionItemStatus.DISMISSED}, synchronize_session=False
            )
            db.commit()
            return True
    return False


def delete_bundle(
    db: Session, batch_id: int | None = None, doc_id: int | None = None
) -> bool:
    """Hard-delete a batch (and all children + files) or a loose document.

    Raises ValueError when the batch is mid-flight (PROCESSING or
    AWAITING_SLICING). Caller maps to HTTP 409.
    """
    from app.services.document_service import DocumentService

    if batch_id:
        batch = db.get(IngestBatch, batch_id)
        if not batch:
            return False
        if batch.status in (
            IngestBatchStatus.PROCESSING,
            IngestBatchStatus.AWAITING_SLICING,
        ):
            raise ValueError(
                f"Cannot delete batch {batch_id} in {batch.status.value} state. "
                "Wait for processing to finish, or retry the bundle first."
            )

        # Snapshot before per-doc loop: delete_document auto-removes the
        # batch row when it deletes the last document, so batch.* lookups
        # would fail on the final iteration.
        raw_source_path = batch.raw_source_path
        # Children-first order. Document.children carries
        # cascade="all, delete-orphan", so deleting a parent first triggers
        # an ORM cascade DELETE on its children before our manual
        # UserReaction / DocumentPin / DocumentRelationship cleanup runs
        # for them — tripping the FK guard.
        sorted_docs = sorted(batch.documents, key=lambda d: (d.parent_id is None, d.id))
        doc_id_list = [d.id for d in sorted_docs]

        # Hard-delete ActionItems sourced from this batch's docs while we
        # can still find them — delete_document nulls source_document_id.
        if doc_id_list:
            db.query(ActionItem).filter(
                ActionItem.source_document_id.in_(doc_id_list)
            ).delete(synchronize_session=False)
            db.commit()

        doc_service = DocumentService(db)
        for did in doc_id_list:
            doc_service.delete_document(did)

        # Defensive: if the batch had zero docs, the per-doc loop never ran
        # and the batch row is still present. Drop it explicitly.
        if not doc_id_list:
            db.query(IngestBatch).filter(IngestBatch.id == batch_id).delete(
                synchronize_session=False
            )
            db.commit()

        if raw_source_path and os.path.exists(raw_source_path):
            try:
                os.remove(raw_source_path)
            except OSError as e:
                logger.warning(
                    f"Failed to delete batch raw source {raw_source_path}: {e}"
                )
        return True

    elif doc_id:
        return DocumentService(db).delete_document(doc_id)

    return False

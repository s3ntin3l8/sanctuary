"""Data & Maintenance settings endpoints."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/maintenance", tags=["settings"])


@router.post("/clear-triage", response_class=HTMLResponse)
def clear_triage_queue(db: Session = Depends(get_db)):
    from sqlalchemy import or_

    from app.models.database import Document, IngestBatch

    triage_docs = (
        db.query(Document)
        .filter(
            or_(
                Document.case_id == "_TRIAGE",
                Document.case_id.is_(None),
            )
        )
        .all()
    )
    batch_ids = {d.ingest_batch_id for d in triage_docs if d.ingest_batch_id}
    doc_ids = [d.id for d in triage_docs]

    for _did in doc_ids:
        db.execute(
            text("DELETE FROM document_vectors WHERE document_id = :id"),
            {"id": _did},
        )

    docs_deleted = len(triage_docs)
    for doc in triage_docs:
        db.delete(doc)

    batches_deleted = 0
    for batch_id in batch_ids:
        remaining = (
            db.query(Document).filter(Document.ingest_batch_id == batch_id).count()
        )
        if remaining == 0:
            batch = db.get(IngestBatch, batch_id)
            if batch:
                db.delete(batch)
                batches_deleted += 1

    db.commit()

    batch_note = (
        f" and {batches_deleted} batch{'es' if batches_deleted != 1 else ''}"
        if batches_deleted
        else ""
    )
    doc_plural = "" if docs_deleted == 1 else "s"
    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-primary)">'
        f"Cleared {docs_deleted} document{doc_plural}{batch_note}."
        f"</span>"
    )


@router.post("/reset-enrichment", response_class=HTMLResponse)
def reset_ai_enrichment(db: Session = Depends(get_db)):
    vectors_cleared = db.execute(text("DELETE FROM document_vectors")).rowcount

    result = db.execute(
        text(
            "UPDATE documents SET "
            "ai_summary = NULL, ai_summary_created_at = NULL, "
            "significance_tier = NULL, key_passages = NULL "
            "WHERE 1=1"
        )
    )
    docs_reset = result.rowcount
    db.commit()

    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-primary)">'
        f"Reset {docs_reset} document{'' if docs_reset == 1 else 's'}; {vectors_cleared} embedding{'' if vectors_cleared == 1 else 's'} cleared."
        f"</span>"
    )
